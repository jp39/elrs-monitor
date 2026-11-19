"""ELRS 4.1 OTA-v4 ISM2G4 sequence and unwrapped slot/hop prediction.

Port of OtaGetUidSeed, random.cpp and FHSSrandomiseFHSSsequenceBuild.
Nonce wraps at 256 slots; the independent sequence wraps at 240 hops.
"""
from dataclasses import dataclass
import math

CHANNEL_COUNT = 80
SEQUENCE_COUNT = 240
SYNC_CHANNEL = 40
FIRST_HZ = 2_400_400_000
SPACING_HZ = 1_000_000
ACQUISITION_CENTER_HZ = 2_440_000_000
CHANNEL_OFFSET_HZ = 400_000


def sequence(uid):
    uid = bytes(uid)
    if len(uid) != 6:
        raise ValueError('FHSS requires a six-byte base UID')
    seed = int.from_bytes(uid[2:5]+bytes((uid[5]^4,)), 'big')
    values = [SYNC_CHANNEL if i%80 == 0 else 0 if i%80 == SYNC_CHANNEL else i%80
              for i in range(SEQUENCE_COUNT)]
    for i in range(SEQUENCE_COUNT):
        if i%80:
            seed = (214013*seed+2531011) & 0x7fffffff
            j = (i//80)*80 + ((seed>>16)%79)+1
            values[i],values[j] = values[j],values[i]
    return tuple(values)


@dataclass(frozen=True)
class HopClock:
    time_s: float
    nonce: int
    index: int
    interval: float
    hop_interval: int

    def slot_at(self, time_s):
        return math.floor((time_s-self.time_s)/self.interval+1e-7)

    def hop_at(self, time_s):
        return (self.nonce%self.hop_interval+self.slot_at(time_s))//self.hop_interval

    def index_at(self, time_s):
        return (self.index+self.hop_at(time_s))%SEQUENCE_COUNT

    def next_boundary(self, time_s):
        return self.time_s+((self.hop_at(time_s)+1)*self.hop_interval
                           -self.nonce%self.hop_interval)*self.interval

    def frequency_at(self, time_s, channels):
        return FIRST_HZ+channels[self.index_at(time_s)]*SPACING_HZ

    @classmethod
    def from_sync(cls, frame, profile):
        sync = frame['sync']
        if not 0 <= sync['fhss_index'] < SEQUENCE_COUNT:
            raise ValueError('Invalid ISM2G4 FHSS sequence index')
        return cls(frame['time_s'],sync['nonce'],sync['fhss_index'],
                   profile.interval,profile.hop_interval)
