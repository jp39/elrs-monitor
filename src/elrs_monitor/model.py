"""Thread-safe presentation state; never invent unseen channel values."""
from collections import deque
from dataclasses import dataclass
import threading
import time

from .profiles import channel_specs, canonical_switch, canonical_rate, get_profile


@dataclass
class Channel:
    number: int
    label: str
    bits: int
    kind: str = 'analog'
    value: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    sample_time: float | None = None

    def update(self, value, sample_time):
        if self.sample_time is not None and sample_time < self.sample_time:
            return
        self.value = int(value)
        self.sample_time = sample_time
        self.minimum = self.value if self.minimum is None else min(self.minimum, self.value)
        self.maximum = self.value if self.maximum is None else max(self.maximum, self.value)

    def snapshot(self, sample_clock, stale_after):
        age = None if self.sample_time is None else max(0, sample_clock-self.sample_time)
        normalized = None
        description = '--'
        if self.value is not None:
            if self.kind == 'armed':
                normalized = float(self.value)
                description = 'ARMED' if self.value else 'DISARMED'
            elif self.bits == 3 and self.kind == 'switch':
                # Hybrid 3-bit switch codes: six positions and special center.
                normalized = .5 if self.value >= 6 else self.value/5
                description = 'MID' if self.value == 7 else ('MID/reserved' if self.value == 6 else str(self.value))
            else:
                normalized = self.value/((1 << self.bits)-1)
                description = str(self.value)
        return dict(number=self.number,label=self.label,bits=self.bits,kind=self.kind,value=self.value,
                    minimum=self.minimum, maximum=self.maximum, age=age,
                    normalized=normalized, description=description,
                    freshness='UNSEEN' if age is None else ('STALE' if age > stale_after else 'fresh'))


class MonitorState:
    def __init__(self, *, source_label, model_id, channel_count, model_match=True,
                 stale_after=1.0,clock=time.monotonic,packet_rate='250Hz',switch_mode='Hybrid',
                 discover_binding=False,identity=None):
        self._lock = threading.Lock()
        self.clock = clock
        self.stale_after = stale_after
        self.source_label = source_label
        self.discover_binding = discover_binding
        self.binding = None
        self.link = None
        self.telemetry = None
        self.telemetry_time = None
        self.identity = dict(identity) if identity is not None else None
        self.model_id = model_id
        self.model_match = model_match
        self.channel_count = channel_count
        self.packet_rate = canonical_rate(packet_rate)
        self.switch_mode = canonical_switch(switch_mode)
        self.switch_mode_label = self.switch_mode
        self.configuration_epoch = None
        self.channels = [Channel(i,'AUX1 / arm' if kind == 'armed' else
                                 (f'CH{i}' if i <= 4 else f'AUX{i-4}'),bits,kind)
                         for i,bits,kind in channel_specs(self.switch_mode)]
        self.armed = Channel(0,'Armed flag',1,'armed')
        self.status = dict(locked=False, counters={}, source_seconds=0.0, processed_through_s=0.0,
                           source_idle_seconds=0.0, application_dropped_seconds=0.0,
                           queue_depth=0, queue_capacity=6, done=False)
        self._status_wall = clock()
        self._frame_wall = None
        self._rc_times = deque(maxlen=1000)
        self.events = deque(maxlen=5)
        self.last_channel_hz = None
        self.cfo_hz = None
        self.error = None
        self.finished = False

    def _configuration(self, info):
        if not info.get('adaptive'):
            return
        rate,mode = info['packet_rate'],info['switch_mode']
        epoch = info.get('configuration_epoch',0)
        if (rate,mode,epoch)!=(self.packet_rate,self.switch_mode,self.configuration_epoch):
            self.packet_rate,self.switch_mode,self.configuration_epoch = rate,mode,epoch
            self.channels = [Channel(i,'AUX1 / arm' if kind=='armed' else
                                     (f'CH{i}' if i<=4 else f'AUX{i-4}'),bits,kind)
                             for i,bits,kind in channel_specs(mode)]
            self.armed = Channel(0,'Armed flag',1,'armed')
            self._rc_times.clear()
            self.telemetry = self.telemetry_time = None
        self.switch_mode_label = info.get('switch_mode_label',mode)

    def on_frame(self, frame):
        with self._lock:
            self._configuration(frame)
            now = self.clock()
            self._frame_wall = now
            self.last_channel_hz = frame['channel_hz']
            self.cfo_hz = frame.get('cfo_hz')
            if frame.get('telemetry',{}).get('kind')=='link_statistics':
                self.telemetry = dict(frame['telemetry'])
                self.telemetry_time = frame['time_s']
            if 'binding' in frame:
                self.binding = dict(frame['binding'])
                b = self.binding
                self.events.append(('info',f"BIND UID {b['uid']} {b['confirmations']}/{b['required']}"
                                    +(' CONFIRMED' if b['confirmed'] else ' candidate')))
            if 'rc' in frame:
                rc = frame['rc']
                t = frame['time_s']
                self.armed.update(int(rc['armed']),t)
                updates = rc.get('channel_updates')
                if updates is None:  # Older saved JSON and original test fixtures.
                    updates = [dict(channel=i+1,value=v) for i,v in enumerate(rc['channels_raw10'])]
                    updates.append(dict(channel=5,value=int(rc['armed'])))
                    if 'auxiliary_channel' in rc:
                        updates.append(dict(channel=rc['auxiliary_channel']+4,value=rc['auxiliary_value']))
                for u in updates:
                    if 1 <= u['channel'] <= len(self.channels):
                        self.channels[u['channel']-1].update(u['value'],t)
                self._rc_times.append(now)
            if 'configuration_change' in frame:
                self.status = dict(self.status,locked=False,
                                   phase='unsupported' if frame['status']=='unsupported_change' else 'reconfiguring')
                self.events.append(('warning',frame['configuration_change']))
            if 'sync' in frame:
                self.link = {k:frame['sync'].get(k) for k in
                             ('link_mode','telemetry_ratio','telemetry_denominator')}
                if 'configuration_change' not in frame:
                    self.events.append(('info', f"SYNC {frame['time_s']:.3f}s  nonce={frame['sync']['nonce']}  identity/CRC OK"))

    def on_status(self, status):
        with self._lock:
            self._configuration(status)
            self.status = {**status, 'counters': dict(status['counters'])}
            if 'link' in status:
                self.link = dict(status['link']) if status['link'] else None
            if 'discover_binding' in status:
                self.discover_binding = status['discover_binding']
            if status.get('identity') is not None:
                self.identity = dict(status['identity'])
            self._status_wall = self.clock()

    def on_message(self, message, level='info'):
        with self._lock:
            self.events.append((level, str(message).replace('\n', ' ')))

    def complete(self, error=None):
        with self._lock:
            self.finished = True
            self.error = error
            self.status['done'] = True
            if error:
                self.events.append(('error', str(error).replace('\n',' ')))

    def reset_ranges(self):
        with self._lock:
            for channel in self.channels:
                channel.minimum = channel.maximum = channel.value
            self.events.append(('info', 'Observed min/max reset; acquisition continues.'))

    def snapshot(self):
        with self._lock:
            now = self.clock()
            # Advance age even if input stalls; freeze at the final sample clock
            # after EOF. Fast replay's read-ahead is deliberately included.
            elapsed = 0 if self.status['done'] else max(0, now-self._status_wall)
            idle = 0 if self.status['done'] else self.status.get('source_idle_seconds',0)
            sample_clock = self.status['source_seconds']+idle+elapsed
            status = {**self.status, 'counters': dict(self.status['counters'])}
            while self._rc_times and self._rc_times[0] < now-1:
                self._rc_times.popleft()
            age = None if self.telemetry_time is None else max(0,sample_clock-self.telemetry_time)
            # Linkstats can legitimately arrive seconds apart at standard ratios.
            # Use announced telemetry cadence, not RC cadence; gaps stay explicit.
            denominator = (self.link or {}).get('telemetry_denominator') or 1
            timeout = max(self.stale_after,3*denominator*get_profile(self.packet_rate).interval)
            telemetry = dict(values=dict(self.telemetry) if self.telemetry is not None else None,
                             age=age,stale_after=timeout,
                             freshness='UNSEEN' if age is None else 'STALE' if age>timeout else 'fresh')
            return dict(link=dict(self.link) if self.link else None,telemetry=telemetry,
                        status=status, channels=[c.snapshot(sample_clock,self.stale_after) for c in self.channels],
                        source_label=self.source_label, model_id=self.model_id,
                        discover_binding=self.discover_binding,
                        identity=dict(self.identity) if self.identity is not None else None,
                        binding=dict(self.binding) if self.binding is not None else None,
                        model_match=self.model_match,packet_rate=self.packet_rate,
                        switch_mode=self.switch_mode,switch_mode_label=self.switch_mode_label,
                        armed=self.armed.snapshot(sample_clock,self.stale_after),
                        channel_count=self.channel_count, stale_after=self.stale_after,
                        events=list(self.events), last_channel_hz=self.last_channel_hz,
                        cfo_hz=self.cfo_hz, output_rc_per_s=len(self._rc_times),
                        last_frame_age=None if self._frame_wall is None else now-self._frame_wall,
                        finished=self.finished, error=self.error,
                        input_idle=status.get('source_idle_seconds',0)+elapsed)
