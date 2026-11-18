"""Synthetic PHY tests. These do not validate the unknown long interleaver."""
import unittest
import numpy as np
from elrs_monitor.phy import (N, RATE, TS, chirp_reference, folded_symbols, recover_payload,
                 sample_fractional)


def modulation(bin_value, polarity=1):
    return np.roll(chirp_reference(), -2*bin_value)**polarity


def packet(payload, polarity=1):
    up = chirp_reference()
    down = up.conj()
    return np.concatenate([np.tile(up, 14), modulation(8), modulation(16),
                           down, down, down[:N//4], up, up,
                           *[modulation(b) for b in payload]])**polarity


class PhyTests(unittest.TestCase):
    def test_symbol_period(self):
        self.assertAlmostEqual(TS, 78.76923076923077e-6)

    def test_all_modulation_bins_both_polarities(self):
        for pol in (1, -1):
            blocks = np.array([modulation(i, pol) for i in range(64)])
            bins, q = folded_symbols(blocks, pol)
            np.testing.assert_array_equal(bins, (pol*np.arange(64)) % 64)
            self.assertTrue(np.all(q > .49))

    def test_fractional_sample_tone(self):
        t = np.arange(2048)
        x = np.exp(2j*np.pi*.08*t)
        y = sample_fractional(x, 100.3, 100)
        expected = np.exp(2j*np.pi*.08*(np.arange(100)+100.3))
        self.assertLess(float(np.max(abs(y-expected))), 1e-3)

    def test_synthetic_packet_with_cfo_and_noise(self):
        rng = np.random.default_rng(50)
        payload = rng.integers(0, 64, 22)
        for pol in (1, -1):
            x = np.r_[np.zeros(256), packet(payload, pol), np.zeros(512)]
            x *= np.exp(2j*np.pi*(-2500)*np.arange(len(x))/RATE)
            x += .01*(rng.standard_normal(len(x))+1j*rng.standard_normal(len(x)))
            result = recover_payload(x, 257, pol)
            np.testing.assert_array_equal(result['payload_bins'], (pol*payload) % 64)
            self.assertEqual(result['sync_bins'], [8, 16] if pol == 1 else [56, 48])
            self.assertLess(abs(result['cfo_hz']+2500), 150)
            self.assertLess(abs(result['corrected_start_sample']-256), .1)


if __name__ == '__main__':
    unittest.main()
