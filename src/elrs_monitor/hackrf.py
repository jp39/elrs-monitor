"""Receive-only libhackrf transport with a continuous sample counter.

Only RX APIs are bound. Control-thread retunes mark uncertainty intervals in
that counter; IQ from those intervals is excluded rather than mislabeled or
stitched to the next frequency. Host timing is an estimate, not a hardware
RF timestamp. CRC/grid/FHSS validation remains mandatory above this layer.
"""
import ctypes as C
import ctypes.util
from dataclasses import dataclass
import math
import queue
import threading
import time

from .capture import Block
from .constants import FS
from .fhss import ACQUISITION_CENTER_HZ


class Transfer(C.Structure):
    _fields_ = [('device',C.c_void_p),('buffer',C.POINTER(C.c_uint8)),
                ('buffer_length',C.c_int),('valid_length',C.c_int),
                ('rx_ctx',C.c_void_p),('tx_ctx',C.c_void_p)]


RXCallback = C.CFUNCTYPE(C.c_int,C.POINTER(Transfer))


class RadioAPI:
    def __init__(self):
        path = ctypes.util.find_library('hackrf')
        if path is None:
            raise ValueError('libhackrf not found; install the HackRF host library')
        self.lib = C.CDLL(path)
        signatures = {
            'hackrf_init': [], 'hackrf_exit': [],
            'hackrf_open': [C.POINTER(C.c_void_p)],
            'hackrf_open_by_serial': [C.c_char_p,C.POINTER(C.c_void_p)],
            'hackrf_close': [C.c_void_p], 'hackrf_stop_rx': [C.c_void_p],
            'hackrf_is_streaming': [C.c_void_p],
            'hackrf_set_sample_rate': [C.c_void_p,C.c_double],
            'hackrf_set_baseband_filter_bandwidth': [C.c_void_p,C.c_uint32],
            'hackrf_set_freq': [C.c_void_p,C.c_uint64],
            'hackrf_set_lna_gain': [C.c_void_p,C.c_uint32],
            'hackrf_set_vga_gain': [C.c_void_p,C.c_uint32],
            'hackrf_set_amp_enable': [C.c_void_p,C.c_uint8],
            'hackrf_set_antenna_enable': [C.c_void_p,C.c_uint8],
            'hackrf_start_rx': [C.c_void_p,RXCallback,C.c_void_p],
        }
        for name,args in signatures.items():
            function = getattr(self.lib,name)
            function.argtypes = args
            function.restype = C.c_int
        self.lib.hackrf_error_name.argtypes = [C.c_int]
        self.lib.hackrf_error_name.restype = C.c_char_p

    def call(self, name, *args):
        code = getattr(self.lib,'hackrf_'+name)(*args)
        if code != 0:
            message = self.lib.hackrf_error_name(code).decode(errors='replace')
            raise RuntimeError(f'hackrf_{name}: {message} ({code})')
        return code


@dataclass
class RadioBlock(Block):
    center_hz: int = ACQUISITION_CENTER_HZ
    epoch: int = 0


@dataclass
class Epoch:
    start: int
    center_hz: int
    number: int
    end: float = math.inf


class NativeSource:
    # Covers estimated USB/PLL timing uncertainty. Excluded time is reported,
    # not counted as host queue loss. Faster rates may miss multiple slots.
    SETTLE_SECONDS = .004

    def __init__(self, args, api=None):
        self.args = args
        self.api = api
        self.device = C.c_void_p()
        self.initialized = False
        self.streaming = False
        self.queue = queue.Queue(maxsize=6)
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.error = None
        self.samples = self.blocks = self.dropped_samples = self.dropped_blocks = 0
        self.retune_discarded_samples = 0
        self.retunes = 0
        self.generation = 0
        self.retune_max_seconds = 0.0
        self.last_read = time.monotonic()
        self.origin = None
        self.center_hz = ACQUISITION_CENTER_HZ
        self.epochs = [Epoch(0,self.center_hz,0)]
        self.lock = threading.Lock()
        self.control_lock = threading.Lock()
        # Keep the callback alive until after hackrf_stop_rx has joined RX.
        self.callback = RXCallback(self._receive)

    def start(self):
        if self.api is None:
            self.api = RadioAPI()
        self.api.call('init');self.initialized = True
        if self.args.serial:
            self.api.call('open_by_serial',self.args.serial.encode(),C.byref(self.device))
        else:
            self.api.call('open',C.byref(self.device))
        for name,value in (('sample_rate',FS),('baseband_filter_bandwidth',8_000_000),
                           ('amp_enable',0),('antenna_enable',0),
                           ('lna_gain',self.args.lna),('vga_gain',self.args.vga),
                           ('freq',self.center_hz)):
            self.api.call('set_'+name,self.device,value)
        self.last_read = time.monotonic()
        self.api.call('start_rx',self.device,self.callback,None)
        self.streaming = True
        return self

    def _enqueue(self, block):
        while not self.stop.is_set():
            try:
                self.queue.put_nowait(block)
                self.blocks += 1
                return
            except queue.Full:
                try:
                    old = self.queue.get_nowait()
                except queue.Empty:
                    continue
                self.dropped_blocks += 1
                self.dropped_samples += len(old.data)//2

    def _receive(self, transfer):
        try:
            if self.stop.is_set():
                return -1
            t = transfer.contents
            if not t.buffer or t.valid_length <= 0 or t.valid_length > t.buffer_length or t.valid_length%2:
                raise RuntimeError('Invalid HackRF IQ transfer length')
            data = C.string_at(t.buffer,t.valid_length)
            now = time.monotonic()
            with self.lock:
                start = self.samples
                self.samples += len(data)//2
                self.last_read = now
                candidate = now-self.samples/FS
                self.origin = candidate if self.origin is None else min(self.origin,candidate)
                end = self.samples
                limit = int(self.args.seconds*FS) if self.args.seconds is not None else None
                if limit is not None:
                    end = min(end,limit)
                segments = []
                for epoch in self.epochs:
                    lo,hi = max(start,epoch.start),min(end,epoch.end)
                    if hi > lo:
                        segments.append(RadioBlock(lo,data[(lo-start)*2:(hi-start)*2],
                                                   epoch.center_hz,epoch.number))
                self.retune_discarded_samples += max(0,end-start)-sum(len(b.data)//2 for b in segments)
                self.epochs = [e for e in self.epochs if e.end > self.samples]
            for block in segments:
                self._enqueue(block)
            if limit is not None and self.samples >= limit:
                self.finished.set()
                return -1
            return 0
        except Exception as exc:
            # ctypes callbacks must never leak exceptions into C.
            self.error = exc
            self.finished.set()
            return -1

    def radio_time(self):
        with self.lock:
            return None if self.origin is None else time.monotonic()-self.origin

    def tune(self, center_hz):
        with self.control_lock:
            if self.stop.is_set() or center_hz == self.center_hz:
                return
            started = time.monotonic()
            with self.lock:
                if self.origin is None:
                    raise RuntimeError('Cannot schedule retune before first IQ transfer')
                cut = max(self.samples,math.floor((started-self.origin)*FS))
                self.epochs[-1].end = cut
            self.api.call('set_freq',self.device,int(center_hz))
            done = time.monotonic()
            with self.lock:
                stable = max(self.samples,math.ceil((done-self.origin+self.SETTLE_SECONDS)*FS))
                self.retunes += 1
                self.generation += 1
                self.center_hz = center_hz
                self.epochs.append(Epoch(stable,center_hz,self.generation))
                self.retune_max_seconds = max(self.retune_max_seconds,done-started)

    def recalibrate(self):
        """Recover host/sample correspondence after suspected stream loss.

        No missing-hardware-sample count can be inferred without timestamps.
        Reset the timing estimate and quarantine, not the absolute byte counter.
        """
        with self.control_lock, self.lock:
            if self.origin is not None:
                self.origin = self.last_read-self.samples/FS
                self.generation += 1
                self.epochs = [Epoch(self.samples+math.ceil(self.SETTLE_SECONDS*FS),
                                     self.center_hz,self.generation)]

    def close(self):
        self.stop.set()
        with self.control_lock:
            if self.streaming:
                # Callback may already have stopped normally at --seconds.
                self.api.lib.hackrf_stop_rx(self.device)
                self.streaming = False
            if self.device.value:
                self.api.lib.hackrf_close(self.device)
                self.device = C.c_void_p()
            if self.initialized:
                self.api.lib.hackrf_exit()
                self.initialized = False
