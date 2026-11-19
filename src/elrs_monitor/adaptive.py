"""Validated live reconfiguration; advertised rate is not necessarily this PHY.

The TX may announce a new rate using its OLD modulation. Such a packet is a
proposal, never a new hop-clock anchor. Only sync decoded on the destination
PHY can resume RC. Model Match/UID restrictions are never relaxed by scanning.
"""
from .follow import TrackingDecoder
from .profiles import PROFILES, get_profile
from .protocol import decode_long_interleaver, validate_crc, decode_sync
from .telemetry import link_details


class AdaptiveTrackingDecoder(TrackingDecoder):
    def __init__(self, config):
        self.settings = dict(config)
        profile = get_profile(config.get('packet_rate','250Hz'))
        # The shared 8/12ch mapping is safe for both. Never infer 8ch from
        # absence of high-group packets: an OTA sync has only one mode bit.
        if profile.full_resolution and config['switch_mode'] in ('8ch','12ch'):
            self.settings['switch_mode'] = '12ch'
        super().__init__(self.settings)
        self.phase = 'awaiting_sync'
        self.reason = None
        self.revision = self.configuration_epoch = 0
        self.not_before = float('-inf')
        self.proposal = None
        self.last_active = None

    @property
    def mode_label(self):
        return '8/12ch' if self.profile.full_resolution and self.switch_mode=='12ch' else self.switch_mode

    @property
    def configuration(self):
        return dict(packet_rate=self.profile.name,switch_mode=self.switch_mode,
                    switch_mode_label=self.mode_label,configuration_epoch=self.configuration_epoch,
                    adaptive=True)

    @property
    def scan_profiles(self):
        return (self.profile,) if self.anchor else (self.profile,)+tuple(
            p for p in PROFILES.values() if p!=self.profile)

    def reset(self):
        super().reset()
        self.proposal = None
        if self.phase!='unsupported':
            self.phase = 'reacquiring'

    def _install(self, profile, mode):
        counts = self.counts
        self.settings.update(packet_rate=profile.name,switch_mode=mode)
        TrackingDecoder.__init__(self,self.settings)
        self.counts = counts
        self.revision += 1

    def _event(self, b, packet, sync, status, reason):
        return dict(time_s=b['time_s'],channel_hz=b['channel_hz'],packet_hex=packet.hex(),
                    packet_type=2,crc_ok=True,identity_match=True,direction='uplink',
                    status=status,sync=dict(sync,**link_details(sync)),
                    configuration_change=reason,observed_phy=b.get('_profile',self.profile.name),
                    **self.configuration)

    def feed(self, b):
        if (b['time_s'] < self.not_before or
                (self.phase=='reconfiguring' and b['time_s']==self.not_before)):
            self.counts['observations_before_configuration'] += 1
            return []
        observed = get_profile(b.get('_profile',self.profile.name))
        try:
            packet,_ = decode_long_interleaver(b['payload_bins'],b['polarity'],observed)
        except ValueError:
            if observed==self.profile:
                return super().feed(b)
            return []
        if packet[0]&3 != 2:
            if observed!=self.profile or self.phase in ('reconfiguring','unsupported'):
                return []
            frames = super().feed(b)
            return [dict(frame,**self.configuration) for frame in frames]
        # Inspect sync without requiring the advertised rate to match the PHY
        # used to decode it. Keep all identity and live-frequency checks.
        if (not validate_crc(packet,self.initializer) or packet[5]!=self.uid4
                or packet[6]!=self.uid5):
            self.counts['adaptive_sync_rejected'] += 1
            return []
        sync = decode_sync(packet)
        check = dict(sync=sync,channel_hz=b['channel_hz'])
        if not self._accept_sync(check):
            return []
        proposed = next((p for p in PROFILES.values() if p.rate_enum==sync['rate_enum']),None)
        if proposed is None or sync['gemini'] or sync['ota_protocol'] not in (0,1):
            reason = ('Gemini is unsupported' if sync['gemini'] else
                      f"Unsupported advertised RF rate enum {sync['rate_enum']}" if proposed is None else
                      f"Unsupported link-mode code {sync['ota_protocol']}")
            if self.phase=='unsupported' and self.reason==reason:
                return []
            self.reset();self.phase='unsupported';self.reason=reason
            self.not_before=b['time_s'];self.revision+=1
            self.counts['unsupported_changes'] += 1
            return [self._event(b,packet,sync,'unsupported_change',reason)]
        mode = ('16ch' if sync['switch_encoding'] else '12ch') if proposed.full_resolution else (
            'Hybrid' if sync['switch_encoding'] else 'Wide')
        key = (proposed.name,mode)
        current = (self.profile.name,self.switch_mode)
        changed = key!=current
        old_phy_announcement = proposed!=observed
        # Confirm same-PHY switch-layout changes twice, as the RX firmware does.
        needs_confirmation = (changed and observed==proposed and observed==self.profile
                              and self.anchor is not None)
        if old_phy_announcement or needs_confirmation:
            repeated = self.proposal==key and not changed
            if not repeated:
                self._install(proposed,mode)
                self.proposal=key;self.phase='reconfiguring';self.reason=f"Awaiting {proposed.name} / {self.mode_label} sync"
                self.not_before=b['time_s']
                self.counts['configuration_announcements'] += 1
                return [self._event(b,packet,sync,'announced_change',self.reason)]
            if old_phy_announcement:
                return []
        elif changed:
            # A fresh sync on its advertised PHY supersedes a missed/cancelled
            # proposal. Do not stay stuck waiting for an abandoned destination.
            self._install(proposed,mode)
            self.proposal=None;self.phase='awaiting_sync';self.reason=None
            self.not_before=b['time_s']
        if self.phase=='reconfiguring' and self.proposal!=key:
            return []
        frames = super().feed(b)
        if not frames:
            return []
        if self.last_active!=key:
            if self.last_active is not None:
                self.counts['configuration_changes'] += 1
            self.configuration_epoch += 1
            self.last_active=key
        self.phase='following';self.reason=None;self.proposal=None
        return [dict(frame,**self.configuration) for frame in frames]
