"""Default live monitor: acquire sync, follow FHSS, reacquire on loss.

Native libhackrf is the default RX transport; SoapySDR is an explicit fallback.
DSP batches contiguous stable frequency spans; an independent thread
controls tuning. Each frequency epoch is filtered separately. Replay remains
in engine.py because legacy raw files contain no retune metadata.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import queue
import threading
import time

from .binding import BindingDiscoveryDecoder
from .capture import Window
from .constants import FS
from .decoder import identity_details
from .engine import process_window,process_acquisition_window,process_profiles_window
from .adaptive import AdaptiveTrackingDecoder
from .fhss import ACQUISITION_CENTER_HZ,CHANNEL_OFFSET_HZ
from .follow import HopFollower
from .hackrf import NativeSource
from .profiles import get_profile
from .protocol import format_uid


def process_radio_window(window, center, epoch, target, binding):
    observations = (process_profiles_window(window,[CHANNEL_OFFSET_HZ],target) if isinstance(target,tuple) else
                    process_acquisition_window(window,[CHANNEL_OFFSET_HZ],target) if binding else
                    process_window(window,[CHANNEL_OFFSET_HZ],target))
    for b in observations:
        # Tag even single-profile jobs: they may finish after a rate change.
        if '_profile' not in b:
            b['_profile'] = target.name
        b['channel_hz'] += center-ACQUISITION_CENTER_HZ
        b['capture_epoch'] = epoch
        b['rx_center_hz'] = center
    return observations


class RadioWindows:
    """Batch a stable RF epoch, not every (possibly tiny) USB fragment.

    Following 50Hz produces one DSP job per hop instead of several overlapping
    fragment jobs. Acquisition is bounded to 40ms cores plus right lookahead.
    Known sample gaps and frequency changes always terminate a window.
    """
    def __init__(self, guard):
        self.guard = guard
        self.core = FS//25
        self.data = bytearray()
        self.left = b''
        self.start = None
        self.center = self.epoch = None

    def _window(self, count, right=0):
        w = Window(self.left+bytes(self.data[:(count+right)*2]),
                   self.start-len(self.left)//2,self.start,self.start+count)
        return w,self.center,self.epoch

    def finish(self):
        result = self._window(len(self.data)//2) if self.data else None
        self.data.clear();self.left = b'';self.start = None
        return result

    def feed(self, block):
        out = []
        if self.start is not None and (block.epoch!=self.epoch or block.center_hz!=self.center
                                      or block.start!=self.start+len(self.data)//2):
            tail = self.finish()
            if tail is not None:
                out.append(tail)
        if self.start is None:
            self.start,self.center,self.epoch = block.start,block.center_hz,block.epoch
        self.data.extend(block.data)
        while len(self.data)//2 >= self.core+self.guard:
            out.append(self._window(self.core,self.guard))
            self.left = bytes(self.data[max(0,self.core-self.guard)*2:self.core*2])
            del self.data[:self.core*2]
            self.start += self.core
        return out


def run_live(args, config, observer, stop=None):
    stop = stop if stop is not None else threading.Event()
    requested = config.get('discover_binding',False)
    decoder = BindingDiscoveryDecoder(config) if requested else AdaptiveTrackingDecoder(config)
    target = get_profile(config.get('packet_rate','250Hz'))
    if args.backend == 'soapy':
        from .soapy import SoapySource
        source = SoapySource(args)
    else:
        source = NativeSource(args)
    follower = None
    identity = identity_details(config)
    learned = None
    confirmations = 0
    guard = math.ceil((max(target.airtime,get_profile('50Hz').airtime)+.001)*FS/400)*400
    assembler = RadioWindows(guard)
    jobs = deque()
    windows = 0
    processed = minimum_time = 0.0
    last_rc = last_gap = None
    max_gap = 0.0
    last_status = 0.0
    dropped = 0
    started = time.monotonic()
    log = None

    def acquiring():
        return requested and learned is None

    def status(done=False):
        following = follower is not None and follower.clock is not None
        counters = dict(decoder.counts)
        if follower is not None:
            for key,value in follower.counts.items():
                counters[key] = counters.get(key,0)+value
        counters['retunes'] = source.retunes
        observer.on_status(dict(
            backend=args.backend,radio_driver=getattr(source,'driver','HackRF'),
            stream_format=getattr(source,'stream_format','CS8'),
            stream_overflows=getattr(source,'stream_overflows',None),
            capture_mode='hop-following',radio_diagnostics=args.radio_diagnostics,phase='binding' if acquiring() else
                (decoder.phase if decoder.phase in ('reconfiguring','unsupported') else
                 'following' if following else 'reacquiring' if follower and follower.phase=='reacquiring'
                 else 'awaiting_sync'),
            adaptive=not acquiring(),configuration_epoch=getattr(decoder,'configuration_epoch',0),
            configuration_reason=getattr(decoder,'reason',None),
            switch_mode_label=getattr(decoder,'mode_label',config['switch_mode']),
            locked=following,counters=counters,identity=identity,
            link=getattr(decoder,'link_status',None),
            discover_binding=acquiring(),discovery_requested=requested,
            discovered_uid=format_uid(learned) if learned is not None else None,
            binding_confirmations=decoder.confirmations if acquiring() else confirmations,
            source_seconds=source.samples/FS,processed_through_s=processed,
            source_idle_seconds=max(0,time.monotonic()-source.last_read),
            queue_depth=source.queue.qsize(),queue_capacity=source.queue.maxsize,
            application_dropped_seconds=source.dropped_samples/FS,
            retune_discarded_seconds=source.retune_discarded_samples/FS,
            retune_max_seconds=source.retune_max_seconds,rx_center_hz=source.center_hz,
            processing_lag_seconds=max(0,source.samples/FS-processed),
            last_rc_gap_seconds=last_gap,max_rc_gap_seconds=max_gap,
            packet_rate=target.name,switch_mode=decoder.switch_mode if not acquiring() else config['switch_mode'],
            windows=windows,elapsed_s=time.monotonic()-started,done=done))

    def report_identity():
        if identity is not None:
            label = (f"Binding phrase {identity['binding_phrase']!r}" if identity['source']=='phrase'
                     else 'Learned UID (zero-padded prefix)' if identity['source']=='binding' else 'Explicit UID')
            observer.on_message(f"{label} -> base UID {identity['uid']}")
            observer.on_message(f"Link UID {identity['link_uid']} | sync UID bytes {identity['sync_uid']}")

    def start_follower():
        return HopFollower(source,target,decoder.channels).start()

    def consume(job):
        nonlocal decoder,learned,confirmations,identity,follower,processed,windows,last_rc,last_gap,max_gap,target
        window,future = job
        for b in future.result():
            if b['time_s'] < minimum_time:
                decoder.counts['observations_before_reacquisition'] += 1
                continue
            if acquiring() and b.get('_profile',decoder.profile.name)!=decoder.profile.name:
                continue
            revision = getattr(decoder,'revision',0)
            for frame in decoder.feed(b):
                if frame.get('configuration_change'):
                    observer.on_message(frame['configuration_change'],'warning')
                frame.update(capture_mode='hop-following')
                if 'rc' in frame and (last_rc is None or frame['time_s']>last_rc):
                    if last_rc is not None:
                        last_gap = frame['time_s']-last_rc
                        max_gap = max(max_gap,last_gap)
                    last_rc = frame['time_s']
                if log is not None:
                    log.write(json.dumps(frame,separators=(',',':'))+'\n')
                observer.on_frame(frame)
            if acquiring() and decoder.confirmed_uid is not None:
                learned = decoder.confirmed_uid
                confirmations = decoder.confirmations
                counts = decoder.counts
                settings = {k:v for k,v in config.items() if k!='discover_binding'}
                settings['uid'] = learned
                decoder = AdaptiveTrackingDecoder(settings)
                decoder.counts = counts
                decoder.counts['handovers'] += 1
                identity = dict(identity_details(settings),source='binding',prefix_reconstructed=True)
                follower = start_follower()
                report_identity()
                observer.on_message('UID learned; scanning for matching normal sync, then following hops.')
            elif not acquiring() and decoder.revision!=revision:
                # A proposal is not a new clock. Stop the old timing scheduler
                # before installing one for the destination PHY. Keep source,
                # sample counter, log and bounded queued jobs intact.
                if follower is not None:
                    follower.close()
                    decoder.counts.update(follower.counts)
                target = decoder.profile
                follower = start_follower()
                status()
            if follower is not None and decoder.anchor is not None:
                follower.track(decoder.anchor)
        decoder.advance(window.core_end/FS)
        if follower is not None and decoder.anchor is None and follower.clock is not None:
            follower.invalidate()
        processed = max(processed,window.core_end/FS)
        windows += 1

    def submit(pool, window, frequency, generation):
        if window is not None:
            profiles = target if acquiring() else (decoder.scan_profiles if decoder.anchor is None else target)
            jobs.append((window,pool.submit(process_radio_window,window,frequency,generation,profiles,acquiring())))
            if len(jobs) >= max(1,args.workers*2):
                consume(jobs.popleft())

    def reacquire(message):
        nonlocal assembler,minimum_time
        source.recalibrate()
        minimum_time = source.radio_time() or source.samples/FS
        decoder.reset()
        assembler = RadioWindows(guard)
        if follower is not None:
            if not follower.lost.is_set():
                follower.invalidate()
            follower.lost.clear()
        observer.on_message(message,'warning')

    try:
        if args.jsonl is not None:
            fd = os.open(args.jsonl,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            log = os.fdopen(fd,'w',buffering=1)
        if stop.is_set():
            return
        report_identity()
        observer.on_message('Receive-only live FHSS following (experimental): acquiring on 2440.4MHz. '
                            'Rate/switch hints adapt from validated sync; searching supported LoRa profiles. '
                            'Retune gaps can miss packets; full-rate reception is not guaranteed.')
        source.start()
        observer.on_message(f"Live RX backend: {args.backend} | "
                            f"{getattr(source,'driver','HackRF')} | "
                            f"{getattr(source,'stream_format','CS8')} at {FS} samples/s")
        if not acquiring():
            follower = start_follower()
        else:
            observer.on_message('Put transmitter into BIND mode. Learned UID will continue to normal hop following.')
        with ThreadPoolExecutor(max_workers=args.workers,thread_name_prefix='elrs-dsp') as pool:
            try:
                while not stop.is_set():
                    if source.error is not None:
                        raise source.error
                    if follower is not None:
                        if follower.error is not None:
                            raise follower.error
                        if follower.lost.is_set():
                            reacquire('Hop lock lost; returning to sync-channel acquisition. UID retained.')
                    if source.dropped_samples != dropped:
                        dropped = source.dropped_samples
                        decoder.counts['host_queue_overruns'] += 1
                        # The callback counter still includes every dropped
                        # queued sample. This is a KNOWN gap, not a loss of the
                        # underlying clock. RadioWindows will not stitch it.
                        # Keep following unless normal validation times out.
                        if acquiring():
                            decoder.reset()
                        observer.on_message('Host IQ queue overflow; retaining hop clock across known sample gap.','warning')
                    while jobs and jobs[0][1].done():
                        consume(jobs.popleft())
                    try:
                        block = source.queue.get(timeout=.005)
                    except queue.Empty:
                        block = None
                    if block is not None:
                        for window,center,epoch in assembler.feed(block):
                            submit(pool,window,center,epoch)
                    if source.finished.is_set() and source.queue.empty():
                        break
                    now = time.monotonic()
                    if now-source.last_read > 5:
                        raise RuntimeError('No IQ data for five seconds; stopping HackRF')
                    if now-last_status >= .1:
                        status();last_status = now
                if not stop.is_set():
                    tail = assembler.finish()
                    if tail is not None:
                        submit(pool,*tail)
                    while jobs:
                        consume(jobs.popleft())
            finally:
                source.stop.set()
                for _,future in jobs:
                    future.cancel()
        if source.error is not None and not stop.is_set():
            raise source.error
        if follower is not None and follower.error is not None and not stop.is_set():
            raise follower.error
        if acquiring() and not stop.is_set():
            raise RuntimeError('No UID discovered before time limit. Put transmitter into BIND mode.')
    finally:
        if follower is not None:
            follower.close()
        source.close()
        if log is not None:
            log.close()
        status(done=True)
