"""FFT channelizer and bounded-window PHY extraction for the live listener.

The overlap supplied by the caller is essential: Fourier filtering treats each
window as periodic. Only bursts starting in the window's non-overlap core are
emitted. The existing packet demodulator remains the validation authority.
"""
import numpy as np
from scipy import fft
from scipy.ndimage import uniform_filter1d

from .constants import FS
from .phy import RATE, chirp_reference, recover_payload
from .profiles import DEFAULT


def fft_channels(raw, offsets):
    if raw.size % 2:
        raise ValueError('Incomplete IQ pair')
    x = raw.astype(np.float32).view(np.complex64) / 128
    # Multiples of 400 input samples give integer output lengths and integer
    # FFT shifts for all supported 1 MHz-spaced ELRS centers ending in .4 MHz.
    padding = (-len(x)) % 400
    if padding:
        x = np.pad(x, (0, padding))
    x -= x.mean()
    n = len(x)
    k = n*13//80
    bins = np.arange(k)
    bins[bins > (k-1)//2] -= k
    frequency = bins*RATE/k
    # Smooth, zero-phase lowpass; flat over the full 812.5 kHz signal.
    transition = np.clip((np.abs(frequency)-425_000)/80_000, 0, 1)
    window = (.5*(1+np.cos(np.pi*transition))).astype(np.float32)
    spectrum = fft.fft(x, workers=1)
    for offset in offsets:
        shift = offset*n/FS
        if abs(shift-round(shift)) > 1e-6:
            raise ValueError('Channel offset is not on the FFT grid')
        translated = spectrum[(bins+round(shift)) % n]*window
        yield offset, fft.ifft(translated, workers=1)*(k/n)


def find_bursts_fast(y, profile=DEFAULT):
    # O(n) moving windows, rather than convolving twice with 64 taps.
    power = uniform_filter1d(abs(y)**2, 64, mode='constant')
    # A followed 250Hz hop is >80% occupied. Even the lower quintile then
    # estimates signal power rather than noise. Use the lower 5% to retain
    # the short inter-packet idle gaps (also handles dense 50Hz binding).
    floor = float(np.percentile(power,5))
    if floor <= 1e-20:
        return []
    mask = power > floor*6
    mask = uniform_filter1d(mask.astype(np.float32), 64, mode='constant') > .5/64
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return [(int(s), int(e), float(np.mean(power[s:e])/floor))
            for s, e in zip(starts, ends)
            if profile.airtime*.8*RATE < e-s < (profile.airtime*1.15+.00008)*RATE]


def fast_preamble(y, start, profile=DEFAULT):
    """Vectorized coarse timing search, then refine promising candidates."""
    n = profile.n
    reference = chirp_reference(profile)
    best = None

    def search(starts, polarity):
        nonlocal best
        starts = np.asarray(starts)
        starts = starts[(starts >= 0) & (starts+10*n <= len(y))]
        if not len(starts):
            return
        samples = y[starts[:, None, None] + np.arange(10)[None, :, None]*n
                    + np.arange(n)[None, None, :]]
        z = abs(fft.fft(samples * reference[None, None, :]**(-polarity), axis=2))**2
        bins = z.argmax(axis=2)
        quality = z.max(axis=2)/np.maximum(z.sum(axis=2), 1e-20)
        coherence = abs(np.mean(np.exp(2j*np.pi*bins/n), axis=1))
        score = quality.mean(axis=1)*coherence
        i = int(score.argmax())
        candidate = (float(score[i]), int(starts[i]), polarity)
        if best is None or candidate[0] > best[0]:
            best = candidate

    step = max(8,n//16)
    coarse = np.arange(max(0,start-n//2),start+3*n//2,step)
    for polarity in (1, -1):
        search(coarse, polarity)
    if best is None or best[0] < .3:
        return None
    _, s, polarity = best
    search(np.arange(s-step,s+step+1,2),polarity)
    return best if best[0] >= .4 else None


def extract_window(raw, window_start, core_start, core_end, offsets, profile=DEFAULT):
    """Yield PHY observations with absolute sample-clock timestamps."""
    for offset, y in fft_channels(raw, offsets):
        for start, end, power in find_bursts_fast(y,profile):
            candidate = fast_preamble(y,start,profile)
            if candidate is None:
                continue
            score, s, polarity = candidate
            expected = [(polarity*b)%profile.bins for b in (8,16)]
            payload = None
            # A repeating preamble can make a neighboring symbol boundary win.
            for boundary in (s,s-profile.n,s+profile.n):
                try:
                    recovered = recover_payload(y,boundary,polarity,profile)
                except ValueError:
                    continue
                if recovered['sync_bins'] == expected:
                    payload = recovered
                    break
            if payload is None:
                continue
            t = window_start/FS + payload['corrected_start_sample']/RATE
            if not core_start/FS <= t < core_end/FS:
                continue
            yield dict(time_s=t, channel_hz=2_440_000_000+offset,
                       polarity=polarity, preamble_score=score,
                       cfo_hz=payload['cfo_hz'],
                       power_to_floor_db=float(10*np.log10(power)),
                       payload_bins=payload['payload_bins'])
