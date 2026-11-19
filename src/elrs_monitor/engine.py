"""Presentation-independent streaming engine with bounded parallel DSP."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import queue
import threading
import time

import numpy as np

from .capture import Source, WindowAssembler
from .constants import FS
from .decoder import LiveDecoder, identity_details
from .binding import BindingDiscoveryDecoder
from .protocol import format_uid
from .dsp import extract_window
from .profiles import DEFAULT, get_profile


def process_window(window, offsets, profile=DEFAULT):
    return sorted(extract_window(np.frombuffer(window.data,dtype=np.int8),
                                 window.start,window.core_start,window.core_end,
                                 offsets,profile),key=lambda b:b['time_s'])


def process_acquisition_window(window, offsets, target):
    """Keep both PHYs available at handover, including already queued jobs.

    For a 50Hz target a single pass serves both phases. Other targets get a
    binding-only pass on the sync channel and a normal-profile pass. Tagging
    prevents using symbols decoded under the wrong profile. Results remain in
    sample-time order so a normal sync later in the confirming core is usable.
    """
    binding = get_profile('50Hz')
    if target == binding:
        return [dict(b,_profile=target.name) for b in process_window(window,offsets,target)]
    observations = [dict(b,_profile=binding.name)
                    for b in process_window(window,[400000],binding)]
    observations += [dict(b,_profile=target.name)
                     for b in process_window(window,offsets,target)]
    return sorted(observations,key=lambda b:(b['time_s'],b['_profile']!=binding.name))


def run_receiver(args, config, observer, stop=None):
    """Run until EOF, cancellation, or error. Observer methods must be brief.

    Discovery hands the learned identity to a fresh known-link decoder without
    stopping/reopening IQ capture or resetting its sample clock. Only a normal
    matching sync can unlock RC parsing. Logging/display survive the transition.
    """
    if not args.replay:
        from .live import run_live
        return run_live(args,config,observer,stop)
    stop = stop if stop is not None else threading.Event()
    discovery_requested = config.get('discover_binding',False)
    decoder = BindingDiscoveryDecoder(config) if discovery_requested else LiveDecoder(config)
    target = get_profile(config.get('packet_rate','250Hz'))
    identity = identity_details(config)
    learned_uid = None
    binding_confirmations = 0

    def acquiring():
        return discovery_requested and learned_uid is None

    source = Source(args)
    # The fixed maximum guard also covers old queued jobs at a rate change.
    airtime = max(target.airtime,decoder.profile.airtime)
    guard = max(FS//100,math.ceil((airtime+.001)*FS/400)*400)
    assembler = WindowAssembler(guard_samples=guard)
    jobs = deque()
    log = None
    started = time.monotonic()
    last_status = 0.0
    windows = 0
    processed = 0.0
    last_rc_time = None
    last_rc_gap = None
    max_rc_gap = 0.0

    def status(done=False):
        observer.on_status(dict(
            locked=decoder.anchor is not None,counters=dict(decoder.counts),
            source_seconds=source.samples/FS,processed_through_s=processed,
            source_idle_seconds=max(0,time.monotonic()-source.last_read),
            queue_depth=source.queue.qsize(),queue_capacity=source.queue.maxsize,
            application_dropped_seconds=source.dropped_samples/FS,
            windows=windows,elapsed_s=time.monotonic()-started,done=done,
            phase='binding' if acquiring() else ('monitoring' if decoder.anchor else 'awaiting_sync'),
            discover_binding=acquiring(),discovery_requested=discovery_requested,identity=identity,
            discovered_uid=format_uid(learned_uid) if learned_uid is not None else None,
            binding_confirmations=decoder.confirmations if acquiring() else binding_confirmations,
            packet_rate=target.name,switch_mode=config.get('switch_mode','Hybrid'),
            processing_lag_seconds=max(0,source.samples/FS-processed),
            last_rc_gap_seconds=last_rc_gap,max_rc_gap_seconds=max_rc_gap,
        ))

    def report_identity():
        if identity is None:
            return
        if identity['source']=='phrase':
            origin = f"Binding phrase {identity['binding_phrase']!r}"
        else:
            origin = 'Learned UID (zero-padded prefix)' if identity['source']=='binding' else 'Explicit UID'
        observer.on_message(f"{origin} -> base UID {identity['uid']}")
        observer.on_message(f"Link UID {identity['link_uid']} | sync UID bytes {identity['sync_uid']} "
                            '(includes Model Match adjustment when enabled)')

    def handover():
        nonlocal decoder,identity,learned_uid,binding_confirmations
        learned_uid = decoder.confirmed_uid
        binding_confirmations = decoder.confirmations
        counters = decoder.counts
        settings = {k:v for k,v in config.items() if k!='discover_binding'}
        settings['uid'] = learned_uid
        decoder = LiveDecoder(settings)
        decoder.counts = counters
        decoder.counts['handovers'] += 1
        identity = dict(identity_details(settings),source='binding',prefix_reconstructed=True)
        report_identity()
        observer.on_message(f'UID learned; continuing {target.name} / {decoder.switch_mode}. '
                            'Waiting for transmitter to leave Bind mode and send a matching normal sync.')
        status()

    def consume(job):
        nonlocal windows,processed,last_rc_time,last_rc_gap,max_rc_gap
        window,future = job
        for b in future.result():
            # Queued acquisition jobs may contain both profiles even after a
            # previous job learned the UID. Never feed cross-profile symbols.
            if b.get('_profile',decoder.profile.name) != decoder.profile.name:
                continue
            for frame in decoder.feed(b):
                if 'rc' in frame and (last_rc_time is None or frame['time_s']>last_rc_time):
                    if last_rc_time is not None:
                        last_rc_gap = frame['time_s']-last_rc_time
                        max_rc_gap = max(max_rc_gap,last_rc_gap)
                    last_rc_time = frame['time_s']
                if log is not None:
                    log.write(json.dumps(frame,separators=(',',':'))+'\n')
                observer.on_frame(frame)
            if acquiring() and decoder.confirmed_uid is not None:
                handover()
        decoder.advance(window.core_end/FS)
        processed = max(processed,window.core_end/FS)
        windows += 1

    def submit(pool,window):
        if window is not None:
            if acquiring():
                future = pool.submit(process_acquisition_window,window,args.offset,target)
            else:
                future = pool.submit(process_window,window,args.offset,target)
            jobs.append((window,future))
            if len(jobs) >= args.workers:
                consume(jobs.popleft())

    try:
        if args.jsonl is not None:
            if args.replay and args.jsonl.resolve() == args.replay.resolve():
                raise ValueError('Frame log must not overwrite replay input')
            fd = os.open(args.jsonl,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            log = os.fdopen(fd,'w',buffering=1)
        if stop.is_set():
            return
        report_identity()
        source.start()
        if discovery_requested:
            observer.on_message('Passive binding acquisition: 50Hz / 2440.4MHz. Put transmitter into BIND mode; '
                                f'then normal monitoring continues at {target.name} / {config["switch_mode"]}.')
        else:
            observer.on_message(f'Receive-only {target.name} / {decoder.switch_mode}; '
                                'waiting for a matching CRC-valid sync packet.')
        if target.name not in ('250Hz','50Hz'):
            observer.on_message('Experimental PHY profile: synthetic-tested, needs physical RF validation.','warning')
        with ThreadPoolExecutor(max_workers=args.workers,thread_name_prefix='elrs-dsp') as pool:
            try:
                while not stop.is_set():
                    try:
                        block = source.queue.get(timeout=.1)
                    except queue.Empty:
                        block = False
                    if block is None:
                        break
                    if block is not False:
                        window,gap = assembler.feed(block)
                        if gap:
                            while jobs:
                                consume(jobs.popleft())
                            # Keep learned_uid, clear only packet/grid state.
                            decoder.reset()
                            observer.on_message(f'IQ gap before {block.start/FS:.3f}s; decoder state reset.','warning')
                        submit(pool,window)
                    now = time.monotonic()
                    if not args.replay and now-source.last_read > 5:
                        raise RuntimeError('No IQ data for five seconds; stopping HackRF')
                    if now-last_status >= .1:
                        status()
                        last_status = now
                if not stop.is_set():
                    submit(pool,assembler.finish())
                    while jobs:
                        consume(jobs.popleft())
            finally:
                if stop.is_set():
                    source.stop.set()
                for _,future in jobs:
                    future.cancel()
        if source.error is not None and not stop.is_set():
            raise source.error
        if acquiring() and not stop.is_set():
            raise RuntimeError('No UID discovered before EOF/time limit. Put the transmitter into '
                               'BIND mode, not just normal 50Hz operation.')
    finally:
        source.close()
        if log is not None:
            log.close()
        status(done=True)
