"""Profile-driven SX1280 LoRa symbol recovery; no packet validation here."""
import numpy as np

from .profiles import BW, RATE, DEFAULT

# Compatibility constants for the original 250Hz test fixtures.
SF = DEFAULT.sf
N = DEFAULT.n
TS = DEFAULT.symbol_seconds
PAYLOAD_START = DEFAULT.payload_start
PAYLOAD_SYMBOLS = len(DEFAULT.symbol_widths)


def chirp_reference(profile=DEFAULT):
    t = np.arange(profile.n)/RATE
    return np.exp(2j*np.pi*(BW/(2*profile.symbol_seconds)*t*t-BW/2*t))


def sample_fractional(y, start, count):
    base = int(np.floor(start))
    frac = start-base
    offsets = np.arange(-16,17)
    if base-16 < 0 or base+count+16 > len(y):
        raise ValueError('Insufficient samples around burst for interpolation')
    weights = np.sinc(frac-offsets)*np.kaiser(len(offsets),8)
    weights /= weights.sum()
    return sum(w*y[base+o:base+o+count] for o,w in zip(offsets,weights))


def tone_frequency(block, polarity, profile=DEFAULT):
    v = block*chirp_reference(profile)**(-polarity)
    z = np.abs(np.fft.fft(v,16384))
    return float(np.fft.fftfreq(len(z),1/RATE)[z.argmax()])


def folded_symbols(blocks, polarity, profile=DEFAULT):
    """Raw modulation bins, not Gray-decoded values. Quality is not SNR."""
    z = abs(np.fft.fft(blocks*chirp_reference(profile)**(-polarity),axis=1))**2
    folded = z[:,:profile.bins]+z[:,profile.bins:]
    bins = folded.argmax(axis=1)
    quality = folded.max(axis=1)/np.maximum(folded.sum(axis=1),1e-20)
    return bins,quality


def recover_payload(y, start, polarity, profile=DEFAULT):
    n = profile.n
    pre = profile.preamble
    if start < 0 or start+(pre+4)*n > len(y):
        raise ValueError('Incomplete preamble/delimiter')
    up = np.mean([tone_frequency(y[start+i*n:start+(i+1)*n],polarity,profile)
                  for i in range(2,pre-2)])
    down = np.mean([tone_frequency(y[start+i*n:start+(i+1)*n],-polarity,profile)
                    for i in (pre+2,pre+3)])
    cfo = float((up+down)/2)
    timing = float((up-down)*n/(2*polarity*BW))
    corrected_start = start-timing
    count = profile.payload_start+len(profile.symbol_widths)*n
    v = sample_fractional(y,corrected_start,count)
    v *= np.exp(-2j*np.pi*cfo*np.arange(len(v))/RATE)
    preamble,preamble_q = folded_symbols(v[:pre*n].reshape(pre,n),polarity,profile)
    sync,sync_q = folded_symbols(v[pre*n:(pre+2)*n].reshape(2,n),polarity,profile)
    extras = v[int((pre+4.25)*n):profile.payload_start].reshape(-1,n)
    extra,extra_q = folded_symbols(extras,polarity,profile)
    payload,quality = folded_symbols(v[profile.payload_start:].reshape(-1,n),polarity,profile)
    return dict(cfo_hz=cfo,timing_adjustment_samples=-timing,
                corrected_start_sample=float(corrected_start),
                preamble_bins=preamble.tolist(),preamble_concentration=np.round(preamble_q,4).tolist(),
                sync_bins=sync.tolist(),sync_concentration=np.round(sync_q,4).tolist(),
                extra_upchirp_bins=extra.tolist(),extra_upchirp_concentration=np.round(extra_q,4).tolist(),
                payload_bins=payload.tolist(),payload_concentration=np.round(quality,4).tolist())
