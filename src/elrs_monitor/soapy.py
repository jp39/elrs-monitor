"""Optional receive-only SoapySDR 0.8 C-API transport.

No Python SoapySDR bindings or GNU Radio required. The DSP contract remains
10Msps signed-int8 IQ. CS8 is preferred; CF32 is quantized if necessary.
Like native HackRF, host/sample timing is estimated and retunes quarantine IQ.
Unknown driver loss fails closed: it must not silently corrupt the hop clock.
"""
import ctypes as C
import ctypes.util
import math
import queue
import threading
import time

import numpy as np

from .constants import FS
from .fhss import ACQUISITION_CENTER_HZ
from .hackrf import Epoch, RadioBlock  # Shared block contract; no libhackrf calls.

RX = 1
TIMEOUT, OVERFLOW = -1, -4
HAS_TIME, END_ABRUPT = 1 << 2, 1 << 3
LogHandler = C.CFUNCTYPE(None, C.c_int, C.c_char_p)


class Kwargs(C.Structure):
    _fields_ = [('size', C.c_size_t), ('keys', C.POINTER(C.c_char_p)),
                ('vals', C.POINTER(C.c_char_p))]


class Range(C.Structure):
    _fields_ = [('minimum', C.c_double), ('maximum', C.c_double), ('step', C.c_double)]


class ArgInfo(C.Structure):
    _fields_ = [(key, C.c_char_p) for key in ('key','value','name','description','units')] + [
        ('type', C.c_int), ('range', Range), ('numOptions', C.c_size_t),
        ('options', C.POINTER(C.c_char_p)), ('optionNames', C.POINTER(C.c_char_p))]


class SoapyAPI:
    """Small RX-only binding. No TX stream/write APIs are bound."""
    def __init__(self):
        path = ctypes.util.find_library('SoapySDR')
        if path is None:
            raise ValueError('SoapySDR library not found; install SoapySDR and its device plugin, '
                             'or use --backend native')
        self.lib = C.CDLL(path)
        self.lib.SoapySDR_getABIVersion.restype = C.c_char_p
        abi = self.lib.SoapySDR_getABIVersion().decode()
        if abi != '0.8':
            raise ValueError(f'SoapySDR ABI 0.8 required by this backend; found {abi}')
        p, z, i, d = C.c_void_p, C.c_size_t, C.c_int, C.c_double
        signatures = {
            'lastStatus': (i, []), 'lastError': (C.c_char_p, []),
            'makeStrArgs': (p, [C.c_char_p]), 'unmake': (i, [p]),
            'getDriverKey': (p, [p]),
            'getStreamFormats': (C.POINTER(C.c_char_p), [p,i,z,C.POINTER(z)]),
            'listGains': (C.POINTER(C.c_char_p), [p,i,z,C.POINTER(z)]),
            'getSettingInfo': (C.POINTER(ArgInfo), [p,C.POINTER(z)]),
            'getFrequencyRange': (C.POINTER(Range), [p,i,z,C.POINTER(z)]),
            'setSampleRate': (i, [p,i,z,d]), 'getSampleRate': (d, [p,i,z]),
            'setBandwidth': (i, [p,i,z,d]),
            'hasGainMode': (C.c_bool, [p,i,z]), 'setGainMode': (i, [p,i,z,C.c_bool]),
            'setGainElement': (i, [p,i,z,C.c_char_p,d]),
            'getGainElement': (d, [p,i,z,C.c_char_p]),
            'writeSetting': (i, [p,C.c_char_p,C.c_char_p]),
            'readSetting': (p, [p,C.c_char_p]),
            'setFrequency': (i, [p,i,z,d,C.POINTER(Kwargs)]),
            'setupStream': (p, [p,i,C.c_char_p,C.POINTER(z),z,C.POINTER(Kwargs)]),
            'getStreamMTU': (z, [p,p]),
            'activateStream': (i, [p,p,i,C.c_longlong,z]),
            'deactivateStream': (i, [p,p,i,C.c_longlong]),
            'closeStream': (i, [p,p]),
            'readStream': (i, [p,p,C.POINTER(p),z,C.POINTER(i),
                                C.POINTER(C.c_longlong),C.c_long]),
        }
        for name, (result, args) in signatures.items():
            f = getattr(self.lib, 'SoapySDRDevice_'+name)
            f.restype, f.argtypes = result, args
        self.lib.SoapySDR_registerLogHandler.argtypes = [LogHandler]
        self.lib.SoapySDR_registerLogHandler.restype = None
        self._log_callback = None
        self.lib.SoapySDR_free.argtypes = [p]
        self.lib.SoapySDR_free.restype = None
        self.lib.SoapySDRStrings_clear.argtypes = [C.POINTER(C.POINTER(C.c_char_p)), z]
        self.lib.SoapySDRStrings_clear.restype = None
        self.lib.SoapySDRArgInfoList_clear.argtypes = [C.POINTER(ArgInfo), z]
        self.lib.SoapySDRArgInfoList_clear.restype = None

    def set_log_handler(self, on_message):
        """Own the process-global logger until RX teardown; never print from C.

        Soapy's default fprintf/SSI output bypasses curses and can scroll its
        physical screen without updating ncurses' cached cursor/screen state.
        Keep the ctypes callback alive, including throughout device destruction.
        """
        if on_message is None:
            self.lib.SoapySDR_registerLogHandler(LogHandler())
            self._log_callback = None
            return

        def receive(level, message):
            try:
                text = (message or b'').decode(errors='replace')[:4096]
                text = ''.join(c if c.isprintable() else ' ' for c in text).strip()
                severity = 'error' if level<=3 else 'warning' if level in (4,9) else 'info'
                if text:
                    on_message('SoapySDR: '+text, severity)
            except Exception:
                # Exceptions escaping a ctypes callback themselves print to
                # stderr. Logging must never break the C boundary or the TUI.
                pass

        callback = LogHandler(receive)
        self.lib.SoapySDR_registerLogHandler(callback)
        self._log_callback = callback

    def get(self, name, *args):
        result = getattr(self.lib, 'SoapySDRDevice_'+name)(*args)
        if self.lib.SoapySDRDevice_lastStatus():
            error = self.lib.SoapySDRDevice_lastError().decode(errors='replace')
            raise RuntimeError(f'SoapySDR {name}: {error}')
        return result

    def call(self, name, *args):
        code = self.get(name, *args)
        if code != 0:
            raise RuntimeError(f'SoapySDR {name} failed ({code})')

    def string(self, name, *args):
        pointer = self.get(name, *args)
        if not pointer:
            return ''
        try:
            return C.string_at(pointer).decode(errors='replace')
        finally:
            self.lib.SoapySDR_free(pointer)

    def strings(self, name, *args):
        count = C.c_size_t()
        values = self.get(name, *args, C.byref(count))
        try:
            return [values[n].decode() for n in range(count.value)]
        finally:
            self.lib.SoapySDRStrings_clear(C.byref(values), count)

    def frequency_ranges(self, device):
        count = C.c_size_t()
        values = self.get('getFrequencyRange', device, RX, 0, C.byref(count))
        try:
            return [(values[n].minimum,values[n].maximum) for n in range(count.value)]
        finally:
            self.lib.SoapySDR_free(values)

    def settings(self, device):
        count = C.c_size_t()
        values = self.get('getSettingInfo', device, C.byref(count))
        try:
            return [values[n].key.decode() for n in range(count.value)]
        finally:
            self.lib.SoapySDRArgInfoList_clear(values, count)


class SoapySource:
    SETTLE_SECONDS = .004

    def __init__(self, args, api=None, on_message=None):
        self.args, self.api = args, api
        self.on_message = on_message
        self._log_redirected = False
        self.device = self.stream = None
        self.streaming = False
        self.thread = None
        self.queue = queue.Queue(maxsize=6)
        self.stop, self.finished = threading.Event(), threading.Event()
        self.error = None
        self.samples = self.blocks = self.dropped_samples = self.dropped_blocks = 0
        self.retune_discarded_samples = self.retunes = self.generation = 0
        self.retune_max_seconds = 0.0
        self.stream_overflows = 0
        self.last_read = time.monotonic()
        self.origin = None
        self.center_hz = ACQUISITION_CENTER_HZ
        self.epochs = [Epoch(0, self.center_hz, 0)]
        self.lock, self.control_lock = threading.Lock(), threading.Lock()
        self.driver = self.stream_format = None
        self._timestamp_origin = None

    def start(self):
        try:
            self._open()
        except Exception:
            try:
                self.close()
            except Exception:
                pass  # Preserve the original startup error.
            raise
        return self

    def _open(self):
        if self.api is None:
            self.api = SoapyAPI()
        if self.on_message is not None:
            self.api.set_log_handler(self.on_message)
            self._log_redirected = True
        selector = self.args.device or 'driver=hackrf'
        if self.args.serial:
            if any(part.strip().startswith('serial=') for part in selector.split(',')):
                raise ValueError('Use either --serial or serial= in --device, not both')
            selector += ',serial='+self.args.serial
        try:
            self.device = self.api.get('makeStrArgs', selector.encode())
        except RuntimeError as exc:
            raise RuntimeError(f'{exc}; check --device and its plugin with SoapySDRUtil --find') from exc
        if not self.device:
            raise RuntimeError('No matching SoapySDR device; check --device and install its '
                               'plugin (SoapyHackRF for HackRF). Use SoapySDRUtil --find.')
        self.driver = self.api.string('getDriverKey', self.device)
        ranges = self.api.frequency_ranges(self.device)
        if not all(any(lo <= hz <= hi for lo,hi in ranges)
                   for hz in range(2_400_000_000,2_480_000_000,1_000_000)):
            raise ValueError(f'SoapySDR RX must tune the entire 2.4GHz hopping plan; '
                             f'driver {self.driver} reports {ranges}')
        formats = self.api.strings('getStreamFormats', self.device, RX, 0)
        self.stream_format = next((f for f in ('CS8','CF32') if f in formats), None)
        if self.stream_format is None:
            raise ValueError(f'SoapySDR driver {self.driver} must support CS8 or CF32 RX; got {formats}')
        self.api.call('setSampleRate', self.device, RX, 0, FS)
        rate = self.api.get('getSampleRate', self.device, RX, 0)
        if not math.isfinite(rate) or abs(rate-FS) > .01:
            raise ValueError(f'SoapySDR must deliver exactly {FS} samples/s; got {rate}')
        self.api.call('setBandwidth', self.device, RX, 0, 8_000_000)
        if self.api.get('hasGainMode', self.device, RX, 0):
            self.api.call('setGainMode', self.device, RX, 0, False)
        gains = self.api.strings('listGains', self.device, RX, 0)
        selected = {'LNA': self.args.lna, 'VGA': self.args.vga} if self.driver.lower() == 'hackrf' else {}
        selected.update(dict(self.args.gain))
        if 'AMP' in gains:
            selected['AMP'] = 0
        for name, value in selected.items():
            if name not in gains:
                raise ValueError(f'SoapySDR gain {name!r} unavailable; available: {gains}')
            self.api.call('setGainElement', self.device, RX, 0, name.encode(), value)
        if 'AMP' in gains and self.api.get('getGainElement', self.device, RX, 0, b'AMP') != 0:
            raise RuntimeError('SoapySDR RF amplifier did not turn off')
        for key in self.api.settings(self.device):
            if key.lower() in ('bias_tx','biastee','bias_tee','bias','antenna_enable'):
                self.api.call('writeSetting', self.device, key.encode(), b'false')
                value = self.api.string('readSetting', self.device, key.encode()).lower()
                if value not in ('false','0','off'):
                    raise RuntimeError(f'SoapySDR antenna bias {key} did not turn off')
        self.api.call('setFrequency', self.device, RX, 0, self.center_hz, None)
        channel = C.c_size_t(0)
        self.stream = self.api.get('setupStream', self.device, RX,
                                   self.stream_format.encode(), C.byref(channel), 1, None)
        if not self.stream:
            raise RuntimeError('SoapySDR could not set up RX stream')
        mtu = self.api.get('getStreamMTU', self.device, self.stream)
        if not 0 < mtu <= 16_777_216:
            raise ValueError(f'Invalid SoapySDR RX MTU: {mtu}')
        self.read_samples = min(mtu, 131_072)
        self.api.call('activateStream', self.device, self.stream, 0, 0, 0)
        self.streaming = True
        self.last_read = time.monotonic()
        thread = threading.Thread(target=self._read, name='soapy-rx', daemon=True)
        thread.start()
        self.thread = thread

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

    def _receive(self, data):
        """Same sample-counter/epoch rules as native; never stitch retune gaps."""
        if not data or len(data)%2:
            raise RuntimeError('Invalid SoapySDR IQ pair count')
        now = time.monotonic()
        with self.lock:
            start = self.samples
            self.samples += len(data)//2
            self.last_read = now
            candidate = now-self.samples/FS
            self.origin = candidate if self.origin is None else min(self.origin, candidate)
            end = self.samples
            limit = int(self.args.seconds*FS) if self.args.seconds is not None else None
            if limit is not None:
                end = min(end, limit)
            segments = []
            for epoch in self.epochs:
                lo, hi = max(start, epoch.start), min(end, epoch.end)
                if hi > lo:
                    segments.append(RadioBlock(lo, data[(lo-start)*2:(hi-start)*2],
                                               epoch.center_hz, epoch.number))
            self.retune_discarded_samples += max(0,end-start)-sum(len(b.data)//2 for b in segments)
            self.epochs = [e for e in self.epochs if e.end > self.samples]
        for block in segments:
            self._enqueue(block)
        if limit is not None and self.samples >= limit:
            self.finished.set()

    def _read(self):
        try:
            width = 2 if self.stream_format == 'CS8' else 8
            buffer = C.create_string_buffer(self.read_samples*width)
            pointers = (C.c_void_p*1)(C.addressof(buffer))
            while not self.stop.is_set() and not self.finished.is_set():
                flags, timestamp = C.c_int(), C.c_longlong()
                n = self.api.get('readStream', self.device, self.stream, pointers,
                                 self.read_samples, C.byref(flags), C.byref(timestamp), 100_000)
                if n == OVERFLOW:
                    self.stream_overflows += 1
                    raise RuntimeError('SoapySDR RX overflow: unknown sample loss; restart acquisition/session')
                if flags.value & END_ABRUPT:
                    raise RuntimeError('SoapySDR abrupt RX end: sample continuity is unknown')
                if n == TIMEOUT or n == 0:
                    continue
                if n < 0 or n > self.read_samples:
                    raise RuntimeError(f'SoapySDR RX discontinuity/error ({n}, flags={flags.value}); '
                                       'stopping rather than trusting the hop clock')
                if flags.value & HAS_TIME:
                    candidate = timestamp.value-(self.samples*1_000_000_000+FS//2)//FS
                    if self._timestamp_origin is None:
                        self._timestamp_origin = candidate
                    elif abs(candidate-self._timestamp_origin) > 2e9/FS:
                        raise RuntimeError('SoapySDR hardware timestamp gap; stopping rather than stitching IQ')
                if self.stream_format == 'CS8':
                    data = buffer.raw[:n*2]
                else:
                    values = np.frombuffer(buffer, dtype=np.float32, count=n*2)
                    if not np.all(np.isfinite(values)):
                        raise RuntimeError('Non-finite SoapySDR CF32 IQ')
                    data = np.clip(np.rint(values*127), -128, 127).astype(np.int8).tobytes()
                self._receive(data)
        except Exception as exc:
            self.error = exc
        finally:
            self.finished.set()

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
                    raise RuntimeError('Cannot schedule retune before first SoapySDR IQ read')
                cut = max(self.samples, math.floor((started-self.origin)*FS))
                self.epochs[-1].end = cut
            self.api.call('setFrequency', self.device, RX, 0, int(center_hz), None)
            done = time.monotonic()
            with self.lock:
                stable = max(self.samples, math.ceil((done-self.origin+self.SETTLE_SECONDS)*FS))
                self.retunes += 1
                self.generation += 1
                self.center_hz = center_hz
                self.epochs.append(Epoch(stable, center_hz, self.generation))
                self.retune_max_seconds = max(self.retune_max_seconds, done-started)

    def recalibrate(self):
        with self.control_lock, self.lock:
            if self.origin is not None:
                self.origin = self.last_read-self.samples/FS
                self.generation += 1
                self.epochs = [Epoch(self.samples+math.ceil(self.SETTLE_SECONDS*FS),
                                     self.center_hz, self.generation)]

    def close(self):
        self.stop.set()
        with self.control_lock:
            if self.thread is not None:
                self.thread.join(timeout=2)
                if self.thread.is_alive():
                    # Do not close/unmake objects still in use by readStream.
                    raise RuntimeError('SoapySDR RX read did not stop within timeout')
                self.thread = None
            errors = []
            for name, args in (
                ('deactivateStream', (self.device,self.stream,0,0) if self.streaming else None),
                ('closeStream', (self.device,self.stream) if self.stream else None),
                ('unmake', (self.device,) if self.device else None)):
                if args is not None:
                    try:
                        self.api.call(name, *args)
                    except Exception as exc:
                        errors.append(exc)
            self.streaming = False
            self.stream = self.device = None
            if self._log_redirected:
                self.api.set_log_handler(None)
                self._log_redirected = False
            if errors:
                raise errors[0]
