"""ELRS 4.1.0 SX1280 2.4GHz LoRa profiles (not FLRC/GFSK).

50Hz/Wide and 250Hz/Hybrid have physical RF regression captures. Other PHY
profiles still require RF validation; geometry and OTA fields follow firmware
and the SX1280 implicit long-interleaved timing formula.
"""
from dataclasses import dataclass
import math

BW = 812_500
RATE = BW*2


@dataclass(frozen=True)
class Profile:
    name: str
    sf: int
    preamble: int
    packet_bytes: int
    code_bits: int
    interval_us: int
    rate_enum: int
    hop_interval: int = 4

    @property
    def bins(self):
        return 1 << self.sf

    @property
    def n(self):
        return 2*self.bins

    @property
    def symbol_seconds(self):
        return self.bins/BW

    @property
    def payload_start(self):
        return int((self.preamble+4.25+(2 if self.sf <= 6 else 0))*self.n)

    @property
    def symbol_widths(self):
        bits = self.packet_bytes*2*self.code_bits
        if self.sf <= 6:
            return (self.sf,)*math.ceil(bits/self.sf)
        # First eight symbols have reduced-rate protection for SF7+.
        first = self.sf-2
        head = min(8, math.ceil(bits/first))
        return (first,)*head+(self.sf,)*math.ceil(max(0,bits-head*first)/self.sf)

    @property
    def airtime(self):
        return (self.payload_start/self.n+len(self.symbol_widths))*self.symbol_seconds

    @property
    def interval(self):
        return self.interval_us/1e6

    @property
    def full_resolution(self):
        return self.packet_bytes == 13

    @property
    def switch_modes(self):
        return ('8ch','12ch','16ch') if self.full_resolution else ('Hybrid','Wide')

    @property
    def anchor_timeout(self):
        return max(2.0, 1.5*80*self.hop_interval*self.interval)


PROFILES = {
    '50Hz': Profile('50Hz',8,12,8,8,20000,21,2),
    '100Hz Full': Profile('100Hz Full',7,12,13,8,10000,23),
    '150Hz': Profile('150Hz',7,12,8,8,6666,24),
    '250Hz': Profile('250Hz',6,14,8,8,4000,27),
    '333Hz Full': Profile('333Hz Full',5,12,13,8,3003,28),
    '500Hz': Profile('500Hz',5,12,8,6,2000,29),
}
DEFAULT = PROFILES['250Hz']
SWITCH_MODES = ('Hybrid','Wide','8ch','12ch','16ch')


def canonical_rate(value):
    key = str(value).lower().replace(' ','')
    for name in PROFILES:
        hz = name.split('Hz')[0]
        aliases = (hz,hz+'hz')
        if 'Full' in name:
            aliases += (hz+'full',hz+'hzfull')
        if key in aliases:
            return name
    raise ValueError('Choose a 2.4GHz LoRa rate: 50, 100, 150, 250, 333, or 500Hz (not D/F modes)')


def canonical_switch(value):
    modes = {m.lower():m for m in SWITCH_MODES}
    key = str(value).lower().replace(' ','')
    if key not in modes:
        raise ValueError('Choose Hybrid, Wide, 8ch, 12ch, or 16ch')
    return modes[key]


def get_profile(value='250Hz'):
    return PROFILES[canonical_rate(value)]


def switch_bit(mode):
    # OTA sync carries only one bit: 8ch and 12ch are indistinguishable there.
    return int(mode in ('Hybrid','16ch'))


def channel_specs(mode):
    if mode in ('Hybrid','Wide'):
        return [(i,10,'analog') for i in range(1,5)]+[(5,1,'armed')]+[
            (i,6 if mode == 'Wide' else (4 if i == 12 else 3),'switch') for i in range(6,13)]
    return [(i,10,'analog') for i in range(1,int(mode[:-2])+1)]
