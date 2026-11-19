"""Validated hop tracking and an independent, interruptible tuning scheduler."""
from collections import Counter
import threading

from .decoder import LiveDecoder
from .telemetry import firmware_downlink_offset, DENOMINATORS
from .hackrf import NativeSource
from .fhss import (HopClock,sequence,FIRST_HZ,SPACING_HZ,SEQUENCE_COUNT,
                   CHANNEL_OFFSET_HZ,ACQUISITION_CENTER_HZ)


class TrackingDecoder(LiveDecoder):
    def __init__(self, config):
        super().__init__(config)
        self.channels = sequence(self.uid)

    def _accept_sync(self, frame):
        index = frame['sync']['fhss_index']
        if (not 0 <= index < SEQUENCE_COUNT or
                frame['channel_hz'] != FIRST_HZ+self.channels[index]*SPACING_HZ):
            self.counts['hop_frequency_failures'] += 1
            return False
        return True

    def _downlink_frequency(self, frame, grid_time):
        clock = HopClock.from_sync(self.anchor,self.profile)
        if frame['channel_hz'] != clock.frequency_at(grid_time,self.channels):
            self.counts['telemetry_hop_failures'] += 1
            return False
        return True

    def _uplink(self, r, p):
        if self.anchor is None:
            return None
        clock = HopClock.from_sync(self.anchor,self.profile)
        slots = round((r['time_s']-clock.time_s)/clock.interval)
        expected_time = clock.time_s+slots*clock.interval
        index = clock.index_at(expected_time)
        if r['channel_hz'] != FIRST_HZ+self.channels[index]*SPACING_HZ:
            self.counts['hop_frequency_failures'] += 1
            return None
        validated = super()._uplink(r,p)
        if validated is not None and r['time_s'] >= self.anchor['time_s']:
            # Keep phase fresh from CRC/nonce/grid/frequency-validated uplinks.
            # This is NOT another SYNC packet and never increments sync count.
            # It avoids losing lock when the missed first slot of a hop carried
            # a sync, and bounds clock drift between received real syncs.
            self.anchor = dict(self.anchor,time_s=r['time_s'],tracking_anchor=True,
                               sync=dict(self.anchor['sync'],nonce=r['inferred_nonce'],fhss_index=index))
        return validated


class HopFollower:
    def __init__(self, source, profile, channels, timeout=.5):
        self.source = source
        self.profile = profile
        self.channels = channels
        self.timeout = max(timeout,6*profile.hop_interval*profile.interval)
        # Firmware hops in TXdone, after the last packet of the old hop,
        # not at the next packet's start. Use that idle guard when available.
        # At 50Hz it gives ~8.7ms lead; faster profiles have much less slack.
        self.tune_lead = max(0,profile.interval-profile.airtime-.0005)
        self.lock = threading.Lock()
        self.clock = None
        self.telemetry_denominator = 1
        self.phase = 'acquiring'
        self.lost = threading.Event()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.error = None
        self.counts = Counter()
        self.last_index = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run,name='elrs-hop-clock',daemon=True)
        self.thread.start()
        return self

    def track(self, anchor):
        with self.lock:
            if self.lost.is_set():
                return
            self.clock = HopClock.from_sync(anchor,self.profile)
            self.telemetry_denominator = DENOMINATORS[anchor['sync'].get('telemetry_ratio_code',0)]
            self.phase = 'following'
        self.wake.set()

    def invalidate(self):
        with self.lock:
            self.clock = None
            self.phase = 'reacquiring'
            self.last_index = None
            self.counts['hop_reacquisitions'] += 1
            self.lost.set()
        self.wake.set()

    def step(self):
        """One scheduling decision; testable without sleeping or real hardware."""
        now = self.source.radio_time()
        with self.lock:
            clock = self.clock
            denominator = self.telemetry_denominator
        if now is None:
            return .005
        if clock is not None and now-clock.time_s > self.timeout:
            self.invalidate()
            clock = None
        if clock is None:
            self.source.tune(ACQUISITION_CENTER_HZ)
            return .01
        tuning_time = now+self.tune_lead
        boundary = clock.next_boundary(tuning_time)
        deadline = boundary-self.tune_lead
        boundary_nonce = (clock.nonce+round((boundary-clock.time_s)/clock.interval))&255
        early = False
        if (denominator>1 and boundary_nonce%denominator==0
                and boundary_nonce%(4*self.profile.hop_interval)==0):
            # Cap early tuning at one in four hop boundaries. At 1:2, moving
            # EVERY hop early could erase every uplink at 50Hz, starving the
            # validated uplink clock. Standard ratios already meet this cap.
            # Preserve the existing quarantine, not uncertain retune samples.
            # Sacrifice preceding uplink IQ only at selected telemetry hops.
            lead = max(self.tune_lead,-firmware_downlink_offset(self.profile)+NativeSource.SETTLE_SECONDS+.001)
            deadline = boundary-lead
            if now>=deadline:
                tuning_time = boundary
                early = True
                deadline = clock.next_boundary(boundary)-self.tune_lead
        index = clock.index_at(tuning_time)
        if self.last_index is not None:
            distance = (index-self.last_index)%SEQUENCE_COUNT
            if distance > 1:
                self.counts['hop_deadlines_skipped'] += distance-1
        if early and index!=self.last_index:
            self.counts['telemetry_early_retunes'] += 1
        self.last_index = index
        center = clock.frequency_at(tuning_time,self.channels)-CHANNEL_OFFSET_HZ
        self.source.tune(center)
        # Re-evaluate after the control call; never chase a stale queued list
        # of frequencies after a delayed deadline or a changed phase anchor.
        return max(.0001,min(.02,deadline-self.source.radio_time()))

    def _run(self):
        try:
            while not self.stop.is_set() and not self.source.finished.is_set():
                self.wake.clear()
                self.wake.wait(self.step())
        except Exception as exc:
            self.error = exc
            self.wake.set()

    def close(self):
        self.stop.set();self.wake.set()
        if self.thread is not None:
            self.thread.join()
