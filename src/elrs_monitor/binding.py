"""Passive OTA-v4 binding discovery. Never transmits or binds a receiver.

ELRS 4.1.0: TX EnterBindingMode uses 50Hz, initial/sync frequency,
OtaCrcInitializer=OTA_VERSION_ID (not <<8), nonce=0. SendUIDOverMSP sends
MSP_ELRS_BIND (0x09) plus UID[2:6], package index 1. RX OnELRSBindMSP
zeroes UID[0:2]. These are effective link identities, not recovered phrases.
"""
from collections import Counter

from .decoder import validate_config
from .profiles import get_profile
from .protocol import decode_long_interleaver, validate_crc, format_uid

BINDING_CHANNEL_HZ = 2_440_400_000
BINDING_COMMAND = 0x09
CONFIRMATIONS = 3
CANDIDATE_TIMEOUT = 2.0
MAX_CANDIDATES = 16


class BindingDiscoveryDecoder:
    """Confirm three distinct repeated binding messages within a short interval.

    Emits explicitly labeled candidate/discovered records, never RC values or
    sync lock. Multiple transmitters are not distinguishable by these messages;
    the first identity to meet the repetition threshold wins.
    """
    def __init__(self, cfg):
        validate_config(cfg)
        if not cfg.get('discover_binding'):
            raise ValueError('Binding discovery must be explicitly selected')
        self.profile = get_profile('50Hz')
        self.switch_mode = 'Hybrid'
        self.anchor = None
        self.confirmed_uid = None
        self.counts = Counter()
        self.candidates = {}

    @property
    def confirmations(self):
        return max((c['count'] for c in self.candidates.values()),default=0)

    def reset(self):
        self.confirmed_uid = None
        self.candidates.clear()
        self.counts['resets'] += 1

    def advance(self, now):
        for uid,c in list(self.candidates.items()):
            if now-c['first'] > CANDIDATE_TIMEOUT:
                del self.candidates[uid]
                self.counts['binding_expired'] += 1

    def feed(self, b):
        if self.confirmed_uid is not None:
            return []
        self.counts['phy_candidates'] += 1
        self.advance(b['time_s'])
        if abs(b['channel_hz']-BINDING_CHANNEL_HZ) > 1000:
            self.counts['binding_wrong_channel'] += 1
            return []
        try:
            packet,corrections = decode_long_interleaver(b['payload_bins'],b['polarity'],self.profile)
        except ValueError:
            self.counts['fec_failures'] += 1
            return []
        # Binding never uses normal UID-dependent CRC or a sync-derived nonce.
        if not validate_crc(packet,4,0):
            self.counts['crc_failures'] += 1
            return []
        if (packet[0]&3 != 1 or packet[1]&127 != 1
                or packet[2] != BINDING_COMMAND or not any(packet[3:7])):
            self.counts['binding_rejected'] += 1
            return []
        uid = b'\0\0'+packet[3:7]
        t = b['time_s']
        candidate = self.candidates.get(uid)
        if candidate is None:
            if len(self.candidates) >= MAX_CANDIDATES:
                del self.candidates[next(iter(self.candidates))]
                self.counts['binding_evictions'] += 1
            candidate = dict(first=t,last=None,count=0)
            self.candidates[uid] = candidate
        # Do not count duplicate DSP observations or alternate polarity fits as
        # independent packets. DATA is normally repeated every other 20ms slot.
        if candidate['last'] is not None and t-candidate['last'] < self.profile.interval/2:
            self.counts['binding_duplicates'] += 1
            return []
        candidate['last'] = t
        candidate['count'] += 1
        confirmed = candidate['count'] >= CONFIRMATIONS
        if confirmed:
            self.confirmed_uid = uid
            self.counts['binding_discovered'] += 1
        self.counts['binding_packets'] += 1
        return [dict(time_s=t,channel_hz=b['channel_hz'],packet_hex=packet.hex(),packet_type=1,
                     fec_corrected_bits=corrections,cfo_hz=b.get('cfo_hz'),crc_ok=True,
                     status='binding_discovered' if confirmed else 'binding_candidate',
                     binding=dict(uid=format_uid(uid),uid_bytes_received=4,
                                  prefix_reconstructed=True,confirmations=candidate['count'],
                                  required=CONFIRMATIONS,confirmed=confirmed))]
