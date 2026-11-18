"""Stateful known-link packet validation for chronological live observations."""
from collections import Counter, deque

from .protocol import (uid_from_phrase, parse_uid, format_uid, decode_long_interleaver,
                       validate_crc, decode_sync, decode_rc, decode_data)
from .profiles import get_profile, canonical_switch, switch_bit


def validate_config(cfg):
    profile = get_profile(cfg.get('packet_rate','250Hz'))
    mode = canonical_switch(cfg.get('switch_mode','Hybrid'))
    if mode not in profile.switch_modes:
        raise ValueError(f'{profile.name} requires switch mode '+', '.join(profile.switch_modes))
    if cfg.get('ota_version') != 4:
        raise ValueError('Only OTA version 4 is supported')
    discover = cfg.get('discover_binding',False)
    if type(discover) is not bool:
        raise ValueError('discover_binding must be a boolean')
    if sum((cfg.get('binding_phrase') is not None,cfg.get('uid') is not None,discover)) != 1:
        raise ValueError('Choose exactly one identity: --bind-phrase, --uid, or --discover-binding')
    if cfg.get('binding_phrase') is not None:
        if not isinstance(cfg['binding_phrase'],str) or not cfg['binding_phrase']:
            raise ValueError('binding_phrase must be a non-empty string')
    if cfg.get('uid') is not None:
        parse_uid(cfg['uid'])
    if type(cfg.get('model_id')) is not int or not 0 <= cfg['model_id'] <= 63:
        raise ValueError('model_id must be an integer in 0..63')
    if type(cfg.get('model_match')) is not bool:
        raise ValueError('model_match must be a boolean')


def identity_details(cfg):
    """Displayable identity seed, base UID, and Model Match-adjusted sync bytes."""
    if cfg.get('discover_binding'):
        return None
    uid = parse_uid(cfg['uid']) if cfg.get('uid') is not None else uid_from_phrase(cfg['binding_phrase'])
    mask = ((~cfg['model_id'])&63) if cfg['model_match'] else 0
    return dict(source='uid' if cfg.get('uid') is not None else 'phrase',
                binding_phrase=cfg.get('binding_phrase'),uid=format_uid(uid),
                link_uid=uid[2:].hex(':'),sync_uid=bytes((uid[4],uid[5]^mask)).hex(':'))


class LiveDecoder:
    def __init__(self, cfg, anchor_timeout=None):
        validate_config(cfg)
        self.profile = get_profile(cfg.get('packet_rate','250Hz'))
        self.switch_mode = canonical_switch(cfg.get('switch_mode','Hybrid'))
        if cfg.get('discover_binding'):
            raise ValueError('Use BindingDiscoveryDecoder for binding discovery')
        uid = parse_uid(cfg['uid']) if cfg.get('uid') is not None else uid_from_phrase(cfg['binding_phrase'])
        self.uid = uid
        self.initializer = ((uid[4] << 8) | uid[5]) ^ (cfg['ota_version'] << 8)
        self.uid4 = uid[4]
        self.uid5 = uid[5] ^ (((~cfg['model_id']) & 63) if cfg['model_match'] else 0)
        self.anchor_timeout = self.profile.anchor_timeout if anchor_timeout is None else anchor_timeout
        self.anchor = None
        self.pending = deque(maxlen=max(512,int(self.anchor_timeout/self.profile.interval)+16))
        self.counts = Counter()

    def reset(self):
        self.anchor = None
        self.pending.clear()
        self.counts['resets'] += 1

    def advance(self, now):
        if self.anchor and now-self.anchor['time_s'] > self.anchor_timeout:
            self.anchor = None
            self.counts['sync_timeouts'] += 1
        while self.pending and now-self.pending[0]['time_s'] > self.anchor_timeout:
            self.pending.popleft()
            self.counts['expired_before_sync'] += 1

    def _uplink(self, r, p):
        if not self.anchor or abs(r['time_s']-self.anchor['time_s']) > self.anchor_timeout:
            return None
        delta = r['time_s']-self.anchor['time_s']
        slots = round(delta/self.profile.interval)
        residual = delta-slots*self.profile.interval
        if abs(residual) > 100e-6:
            self.counts['off_grid'] += 1
            return None
        nonce = (self.anchor['sync']['nonce']+slots) & 255
        if not validate_crc(p, self.initializer, nonce):
            self.counts['crc_failures'] += 1
            return None
        r.update(crc_ok=True, inferred_nonce=nonce,
                 slot_timing_error_us=residual*1e6, status='validated_uplink')
        if r['packet_type'] == 0:
            try:
                r['rc'] = decode_rc(p,self.switch_mode,nonce)
            except ValueError:
                self.counts['mode_failures'] += 1
                return None
            self.counts['rc'] += 1
        else:
            r['data'] = decode_data(p)
            self.counts['data'] += 1
        return r

    def feed(self, b):
        self.counts['phy_candidates'] += 1
        self.advance(b['time_s'])
        try:
            p, corrections = decode_long_interleaver(b['payload_bins'],b['polarity'],self.profile)
        except ValueError:
            self.counts['fec_failures'] += 1
            return []
        r = dict(time_s=b['time_s'], channel_hz=b['channel_hz'],
                 packet_hex=p.hex(), packet_type=p[0] & 3,
                 fec_corrected_bits=corrections, cfo_hz=b.get('cfo_hz'),
                 crc_ok=False)
        if r['packet_type'] == 2:
            if not validate_crc(p, self.initializer):
                self.counts['crc_failures'] += 1
                return []
            if p[5] != self.uid4 or p[6] != self.uid5:
                self.counts['identity_failures'] += 1
                return []
            sync = decode_sync(p)
            # otaProtocol is ELRS's link-mode enum, NOT the OTA version:
            # TX_NORMAL_MODE=0 and TX_MAVLINK_MODE=1 are both valid uplinks.
            if (sync['rate_enum'] != self.profile.rate_enum
                    or sync['switch_encoding'] != switch_bit(self.switch_mode)
                    or sync['ota_protocol'] not in (0,1) or sync['gemini']):
                self.reset()
                self.counts['unsupported_sync'] += 1
                return []
            r.update(sync=sync, crc_ok=True, identity_match=True, status='validated_sync')
            self.anchor = r
            self.counts['sync'] += 1
            out = []
            while self.pending:
                waiting = self.pending.popleft()
                validated = self._uplink(waiting, bytes.fromhex(waiting['packet_hex']))
                if validated:
                    out.append(validated)
            out.append(r)
            return sorted(out, key=lambda x: x['time_s'])
        if r['packet_type'] not in (0, 1):
            self.counts['unsupported_type'] += 1
            return []
        if not self.anchor:
            if len(self.pending) == self.pending.maxlen:
                self.counts['pending_overflows'] += 1
            self.pending.append(r)
            return []
        validated = self._uplink(r, p)
        return [validated] if validated else []
