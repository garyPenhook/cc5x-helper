"""test_brg.py -- pin the EUSART SPBRG computation to known datasheet rows (P3).

The expected SPBRG values are taken from the Microchip "EUSART Baud Rate
Generator" tables (PIC16F15213/14/23/24/43/44, covers PIC16F15244), confirmed via
the Microchip MCP. If compute_brg ever drifts from the datasheet, these fail.
"""
from __future__ import annotations

import unittest

from cc5x_setcc_native_lib import brg


class DatasheetRows(unittest.TestCase):
    def _check(self, fosc, baud, spbrg, brgh, err_pct):
        sol = brg.compute_brg(fosc, baud)
        self.assertEqual(sol.spbrg, spbrg, f"SPBRG for {fosc}/{baud}")
        self.assertEqual(sol.brgh, brgh, f"BRGH for {fosc}/{baud}")
        self.assertFalse(sol.brg16)
        self.assertAlmostEqual(sol.error_pct, err_pct, places=2)

    # BRGH=0 rows (÷64): the divisor table's 8-bit/BRGH=0 column.
    def test_32mhz_9600_brgh0(self):
        self._check(32_000_000, 9600, spbrg=51, brgh=False, err_pct=0.16)

    def test_8mhz_9600_brgh0(self):
        self._check(8_000_000, 9600, spbrg=12, brgh=False, err_pct=0.16)

    def test_18432khz_9600_exact(self):
        self._check(18_432_000, 9600, spbrg=29, brgh=False, err_pct=0.00)

    def test_1mhz_2400_brgh1(self):
        self._check(1_000_000, 2400, spbrg=25, brgh=True, err_pct=0.16)

    # BRGH=1 rows (÷16): chosen when ÷64 has no good fit or larger error.
    def test_4mhz_9600_brgh1(self):
        self._check(4_000_000, 9600, spbrg=25, brgh=True, err_pct=0.16)

    def test_32mhz_115200_brgh1(self):
        self._check(32_000_000, 115200, spbrg=16, brgh=True, err_pct=2.12)

    def test_11_0592mhz_115200_exact(self):
        # 11.0592 MHz is a classic UART-exact crystal: 11.0592e6/(16*(5+1)) = 115200.
        self._check(11_059_200, 115200, spbrg=5, brgh=True, err_pct=0.00)


class Selection(unittest.TestCase):
    def test_picks_minimum_error_mode(self):
        # 4 MHz/9600: BRGH=0 (÷64) is a poor fit (~-7%), BRGH=1 (÷16) is +0.16%;
        # the low-error BRGH=1 mode must win.
        sol = brg.compute_brg(4_000_000, 9600)
        self.assertTrue(sol.brgh)
        self.assertLess(abs(sol.error_pct), 1.0)

    def test_exact_crystal_is_zero_error(self):
        # 11.0592 MHz / 9600 = 64*18 exactly -> BRGH=0, n=17, 0% error.
        sol = brg.compute_brg(11_059_200, 9600)
        self.assertEqual((sol.spbrg, sol.brgh), (17, False))
        self.assertEqual(sol.actual_baud, 9600)

    def test_ties_prefer_brgh0(self):
        # 32 MHz/9600: ÷64,n=51 and ÷16,n=207 give the identical divisor (3328) and
        # identical error -- the 8-bit/BRGH=0 form is kept.
        self.assertFalse(brg.compute_brg(32_000_000, 9600).brgh)


class BrghAvailability(unittest.TestCase):
    def test_allow_brgh_false_restricts_to_div64(self):
        # 4 MHz/9600 is best in ÷16 (BRGH=1); with allow_brgh=False only ÷64 is tried,
        # so the returned mode must be BRGH=0 even though it fits poorly.
        sol = brg.compute_brg(4_000_000, 9600, allow_brgh=False)
        self.assertFalse(sol.brgh)
        self.assertEqual(sol.multiplier, 64)

    def test_allow_brgh_false_ok_when_div64_fits(self):
        sol = brg.compute_brg(32_000_000, 9600, allow_brgh=False)
        self.assertEqual((sol.spbrg, sol.brgh), (51, False))


class SixteenBit(unittest.TestCase):
    """Pin the 16-bit BRG (BRG16=1) to the datasheet's SYNC=0 tables. n is the full
    SPxBRGH:SPxBRGL pair; the byte split is exposed via spbrgl/spbrgh."""

    def test_default_stays_8bit(self):
        # Without allow_brg16, a rate that fits 8-bit must resolve 8-bit (unchanged).
        sol = brg.compute_brg(32_000_000, 9600)
        self.assertFalse(sol.brg16)
        self.assertEqual(sol.spbrgh, 0)

    def test_16bit_slow_baud_min_error(self):
        # 32 MHz / 300: 8-bit overflows; the lowest-error 16-bit mode is BRG16=1/
        # BRGH=1 (F/[4(n+1)]), n=26666 -- the datasheet's BRGH=1/BRG16=1 300 row.
        sol = brg.compute_brg(32_000_000, 300, allow_brg16=True)
        self.assertTrue(sol.brg16)
        self.assertTrue(sol.brgh)
        self.assertEqual(sol.spbrg, 26666)
        self.assertEqual((sol.spbrgh, sol.spbrgl), (0x68, 0x2A))  # 26666 = 0x682A
        self.assertAlmostEqual(sol.error_pct, 0.0, places=2)

    def test_16bit_brgh0_via_div16(self):
        # Forcing BRGH=0 (÷16) selects the datasheet's BRG16=1/BRGH=0 row: 300 -> 6666.
        sol = brg.compute_brg(32_000_000, 300, allow_brgh=False, allow_brg16=True)
        self.assertEqual((sol.brg16, sol.brgh, sol.spbrg), (True, False, 6666))
        self.assertEqual((sol.spbrgh, sol.spbrgl), (6666 >> 8, 6666 & 0xFF))

    def test_16bit_brgh1_high_baud(self):
        # 32 MHz / 115200: BRG16=1/BRGH=1 row gives n=68 (F/[4(n+1)]), 0.64% error.
        sol = brg.compute_brg(32_000_000, 115200, allow_brg16=True)
        self.assertTrue(sol.brg16)
        self.assertTrue(sol.brgh)
        self.assertEqual(sol.spbrg, 68)
        self.assertEqual((sol.spbrgh, sol.spbrgl), (0, 68))
        self.assertAlmostEqual(sol.error_pct, 0.64, places=2)

    def test_16bit_brgh1_midband(self):
        # 32 MHz / 2400: BRG16=1/BRGH=1 row gives n=3332; the finer 16-bit divisor
        # wins on error over the in-range 8-bit fit.
        sol = brg.compute_brg(32_000_000, 2400, allow_brg16=True)
        self.assertTrue(sol.brg16)
        self.assertTrue(sol.brgh)
        self.assertEqual(sol.spbrg, 3332)
        self.assertEqual((sol.spbrgh, sol.spbrgl), (3332 >> 8, 3332 & 0xFF))


class OutOfRange(unittest.TestCase):
    def test_too_slow_for_8bit_raises(self):
        # 300 baud at 32 MHz needs n>255 in both 8-bit modes -> 16-bit territory.
        with self.assertRaises(ValueError):
            brg.compute_brg(32_000_000, 300)

    def test_too_slow_resolves_with_brg16(self):
        # The same rate is reachable once the 16-bit BRG is allowed.
        sol = brg.compute_brg(32_000_000, 300, allow_brg16=True)
        self.assertTrue(sol.brg16)

    def test_nonpositive_raises(self):
        with self.assertRaises(ValueError):
            brg.compute_brg(0, 9600)
        with self.assertRaises(ValueError):
            brg.compute_brg(32_000_000, 0)


if __name__ == "__main__":
    unittest.main()
