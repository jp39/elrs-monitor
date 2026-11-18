"""Streaming, acquisition, discontinuity, and FFT channelizer tests."""
import io
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from elrs_monitor.protocol import crc14, uid_from_phrase
from elrs_monitor.decoder import LiveDecoder
from elrs_monitor.dsp import extract_window, fft_channels
from elrs_monitor.capture import Block, WindowAssembler, Source, BLOCK_SAMPLES, FS
from test_packets import encode_for_test
from test_phy import packet as phy_packet

CONFIG = dict(binding_phrase='test-only-phrase', model_id=11, model_match=True,
              packet_rate='250Hz', switch_mode='Hybrid', ota_version=4)


def seal(data, nonce=0):
    uid = uid_from_phrase(CONFIG['binding_phrase'])
    initializer = ((uid[4] << 8) | uid[5]) ^ 0x400
    data = bytearray(data)
    crc = crc14(data[:7], initializer ^ nonce)
    data[0] |= (crc >> 8) << 2
    data[7] = crc & 255
    return bytes(data)


def sync_packet(nonce=1, model=11, rate=27):
    uid = uid_from_phrase(CONFIG['binding_phrase'])
    return seal(bytes([2,0,nonce,rate,0x01,uid[4],uid[5]^((~model)&63),0]))


def rc_packet(nonce=0):
    return seal(bytes([0,0,2,24,192,127,0,0]), nonce)


def observation(data, t):
    return dict(time_s=t, channel_hz=2_440_400_000, polarity=1,
                payload_bins=encode_for_test(data), cfo_hz=-2500)


class LiveStateTests(unittest.TestCase):
    def test_acquisition_validates_bounded_pre_sync_packets(self):
        decoder = LiveDecoder(CONFIG)
        self.assertEqual(decoder.feed(observation(rc_packet(0), 0)), [])
        frames = decoder.feed(observation(sync_packet(1), .004))
        self.assertEqual([r['packet_type'] for r in frames], [0,2])
        self.assertTrue(all(r['crc_ok'] for r in frames))
        frames = decoder.feed(observation(rc_packet(2), .008))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]['inferred_nonce'], 2)

    def test_wrong_model_and_wrong_phrase(self):
        decoder = LiveDecoder(CONFIG)
        self.assertEqual(decoder.feed(observation(sync_packet(model=12), .004)), [])
        self.assertIsNone(decoder.anchor)
        decoder = LiveDecoder({**CONFIG, 'binding_phrase':'wrong-test-phrase'})
        self.assertEqual(decoder.feed(observation(sync_packet(), .004)), [])
        self.assertIsNone(decoder.anchor)

    def test_gap_resets_nonce_grid(self):
        decoder = LiveDecoder(CONFIG)
        decoder.feed(observation(sync_packet(), .004))
        decoder.reset()
        self.assertIsNone(decoder.anchor)
        self.assertEqual(decoder.feed(observation(rc_packet(2), .008)), [])

    def test_stale_anchor_and_pending_expiry(self):
        decoder = LiveDecoder(CONFIG)
        decoder.feed(observation(sync_packet(), .004))
        decoder.advance(3)
        self.assertIsNone(decoder.anchor)
        decoder.feed(observation(rc_packet(0), 3.0))
        self.assertEqual(len(decoder.pending), 1)
        decoder.advance(6)
        self.assertEqual(len(decoder.pending), 0)

    def test_off_grid_and_bad_nonce_rejected(self):
        decoder = LiveDecoder(CONFIG)
        decoder.feed(observation(sync_packet(), .004))
        self.assertEqual(decoder.feed(observation(rc_packet(2), .009)), [])
        self.assertEqual(decoder.feed(observation(rc_packet(3), .008)), [])
        self.assertEqual(decoder.counts['off_grid'], 1)
        self.assertEqual(decoder.counts['crc_failures'], 1)

    def test_unsupported_mode_releases_lock(self):
        decoder = LiveDecoder(CONFIG)
        decoder.feed(observation(sync_packet(), .004))
        self.assertEqual(decoder.feed(observation(sync_packet(rate=29), .008)), [])
        self.assertIsNone(decoder.anchor)


class StreamingTests(unittest.TestCase):
    def test_windows_and_gaps(self):
        assembler = WindowAssembler()
        a = Block(0, b'\x00\x00'*BLOCK_SAMPLES)
        b = Block(a.end, a.data)
        c = Block(b.end+BLOCK_SAMPLES, a.data)
        self.assertEqual(assembler.feed(a), (None, False))
        window, gap = assembler.feed(b)
        self.assertFalse(gap)
        self.assertEqual((window.start,window.core_start,window.core_end), (0,0,a.end))
        self.assertEqual(assembler.feed(c), (None, True))
        final = assembler.finish()
        self.assertEqual(final.start, c.start)
        self.assertEqual(final.data, c.data)

    def test_partial_reads_preserve_iq_pairing(self):
        class ShortReads(io.BytesIO):
            def read(self, n=-1):
                return super().read(min(n,701))
        args = SimpleNamespace(replay=Path('synthetic'), seconds=None, pace=False)
        source = Source(args)
        raw = bytes(range(256))*100
        source.stream = ShortReads(raw)
        source._read()
        block = source.queue.get_nowait()
        self.assertEqual(block.data, raw)
        self.assertIsNone(source.queue.get_nowait())
        self.assertIsNone(source.error)

    def test_live_queue_is_bounded_and_counts_drops(self):
        source = Source(SimpleNamespace())
        for i in range(10):
            source._put(Block(i*10,b'\x00\x00'*10), drop=True)
        self.assertEqual(source.queue.qsize(),6)
        self.assertEqual(source.dropped_blocks,4)
        self.assertEqual(source.dropped_samples,40)
        self.assertEqual(source.queue.get_nowait().start,40)

    def test_truncated_iq_reports_error(self):
        args = SimpleNamespace(replay=Path('synthetic'), seconds=None, pace=False)
        source = Source(args)
        source.stream = io.BytesIO(b'\x00'*3)
        source._read()
        self.assertIsInstance(source.error,ValueError)
        self.assertIsNone(source.queue.get_nowait())

    def test_fft_channelizer_preserves_tone(self):
        count = 40_000
        t = np.arange(count)/FS
        x = .5*np.exp(2j*np.pi*500_000*t)
        raw = np.empty(count*2,dtype=np.int8)
        raw[::2] = np.round(x.real*128)
        raw[1::2] = np.round(x.imag*128)
        _, y = next(fft_channels(raw,[400_000]))
        observed = np.angle(np.sum(y[1:]*y[:-1].conj()))*1_625_000/(2*np.pi)
        self.assertAlmostEqual(observed,100_000,delta=100)
        self.assertAlmostEqual(float(np.mean(abs(y))),.5,delta=.02)

    def test_synthetic_rf_packet_spanning_core_boundary(self):
        # Build a 10 Msps capture by Fourier resampling the synthetic PHY,
        # translate to the sync channel, and quantize like the HackRF.
        from scipy.signal import resample_poly
        from elrs_monitor.phy import RATE
        bins = encode_for_test(sync_packet())
        base = np.r_[np.zeros(256),phy_packet(bins),np.zeros(512)]
        x = resample_poly(base,80,13)
        total = 2_000_000  # 200 ms
        capture = np.zeros(total,dtype=np.complex64)
        at = 985_000  # preamble begins just before 100 ms
        capture[at:at+len(x)] = .3*x
        capture *= np.exp(2j*np.pi*400_000*np.arange(total)/FS)
        raw = np.empty(total*2,dtype=np.int8)
        raw[::2] = np.clip(np.rint(capture.real*128),-128,127)
        raw[1::2] = np.clip(np.rint(capture.imag*128),-128,127)
        # A tiny deterministic noise floor is needed by energy detection.
        rng=np.random.default_rng(2)
        raw += rng.integers(-1,2,len(raw),dtype=np.int8)
        out=[]
        for core in (0,BLOCK_SAMPLES):
            lo=max(0,core-100_000);hi=min(total,core+BLOCK_SAMPLES+100_000)
            out.extend(extract_window(raw[lo*2:hi*2],lo,core,core+BLOCK_SAMPLES,[400_000]))
        self.assertEqual(len(out),1)
        from elrs_monitor.protocol import decode_long_interleaver
        recovered,_=decode_long_interleaver(out[0]['payload_bins'])
        self.assertEqual(recovered,sync_packet())


if __name__ == '__main__':
    unittest.main()
