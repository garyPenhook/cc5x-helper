"""test_measure.py -- parser tests for the P2 measure gate (measure.py).

Run against golden fixtures that are *real* CC5X 3.8 output (tests/golden/measure/,
a small stub with two globals and a 3-deep call chain), so the parsers stay pinned
to the actual report format. Pure data: no compiler, no packs.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from cc5x_setcc_native_lib import debuggen as dg
from cc5x_setcc_native_lib import measure as m

GOLDEN = Path(__file__).resolve().parent / "golden" / "measure"


def _caps(ram_bytes=512, flash_words=8192):
    return dg.DeviceCaps(ram_bytes=ram_bytes, flash_words=flash_words, has_eusart=True,
                         has_free_timer=True, eusart=None, arch="PIC14E")


def _provisional(tier="full"):
    return dg.TierDecision(tier=tier, provisional=True, forced=False, reason="auto: test")


class OccParser(unittest.TestCase):
    def test_real_fixture(self):
        rep = m.parse_occ((GOLDEN / "stub_16f1509.occ").read_text())
        self.assertEqual(rep.chip, "16F1509")
        self.assertEqual((rep.ram_used, rep.ram_free), (21, 491))
        self.assertEqual(rep.ram_total, 512)            # 16F1509 has 512 B RAM
        self.assertEqual((rep.code_words, rep.code_pct), (18, 0))

    def test_truncated_occ_raises(self):
        with self.assertRaises(ValueError):
            m.parse_occ("Chip = 16F1509\n(compile died before the summary)\n")


class VarParser(unittest.TestCase):
    def setUp(self):
        self.syms = m.parse_var((GOLDEN / "stub_16f1509.var").read_text())
        self.by_name = {s.name: s for s in self.syms}

    def test_globals_resolved_to_addresses(self):
        # The stub's own globals must be found at the addresses CC5X assigned.
        self.assertEqual(self.by_name["cdl_seq"].address, 0x023)
        self.assertEqual(self.by_name["cdl_seq"].cls, "G")
        ring = self.by_name["cdl_ring"]
        self.assertEqual((ring.address, ring.size, ring.cls), (0x024, 16, "G"))

    def test_bitfield_address_and_bit_split(self):
        # SFR bit fields come through with a separate bit index and size 0.
        carry = self.by_name["Carry"]
        self.assertEqual((carry.address, carry.bit, carry.size), (0x003, 0, 0))

    def test_predefined_sfrs_present(self):
        self.assertEqual(self.by_name["STATUS"].address, 0x003)
        self.assertEqual(self.by_name["BSR"].cls, "P")


class FcsParser(unittest.TestCase):
    def test_call_depth(self):
        rep = m.parse_fcs((GOLDEN / "stub_16f1509.fcs").read_text())
        # main -> cdl_send -> cdl_crc8 == 3 stack levels (L0..L2).
        self.assertEqual(rep.max_depth, 3)
        self.assertIn("cdl_crc8", rep.functions)

    def test_empty_fcs(self):
        self.assertEqual(m.parse_fcs("* FUNCTION CALL STRUCTURE\n").max_depth, 0)


class MeasuredGate(unittest.TestCase):
    """The 02 §6 tier-confirmation gate: confirm/demote from a measured .occ/.fcs."""

    def setUp(self):
        self.occ = m.parse_occ((GOLDEN / "stub_16f1509.occ").read_text())   # 21/512 B, 18 w
        self.fcs = m.parse_fcs((GOLDEN / "stub_16f1509.fcs").read_text())   # depth 3
        self.var = m.parse_var((GOLDEN / "stub_16f1509.var").read_text())

    def test_confirm_when_budget_fits(self):
        out = dg.confirm_tier(_provisional("full"), _caps(), self.occ)
        self.assertEqual(out.tier, "full")
        self.assertFalse(out.provisional)                 # confirmed by measurement
        self.assertIn("measured", out.reason)

    def test_demote_on_ram_overrun(self):
        # 400 B stub of 512 B leaves < 50% for the app -> demote full -> min.
        occ = m.OccReport(chip="16F1509", ram_used=400, ram_free=112, code_words=18, code_pct=0)
        out = dg.confirm_tier(_provisional("full"), _caps(), occ)
        self.assertEqual(out.tier, "min")
        self.assertTrue(out.provisional)                  # lower tier not yet measured
        self.assertIn("RAM", out.reason)
        self.assertIn("re-measure at 'min'", out.reason)

    def test_demote_on_flash_overrun(self):
        occ = m.OccReport(chip="16F1509", ram_used=10, ram_free=502, code_words=5000, code_pct=99)
        out = dg.confirm_tier(_provisional("full"), _caps(flash_words=4096), occ)
        self.assertEqual(out.tier, "min")
        self.assertIn("flash", out.reason)

    def test_stack_check_is_opt_in_and_can_demote(self):
        # Skipped unless an explicit hardware-stack depth is supplied (never assumed).
        self.assertFalse(dg.confirm_tier(_provisional("full"), _caps(), self.occ).provisional)
        # depth 3 > a 2-deep stack -> demote; 3 <= 16 -> confirm.
        demoted = dg.confirm_tier(_provisional("full"), _caps(), self.occ,
                                  fcs=self.fcs, hw_stack_depth=2)
        self.assertTrue(demoted.provisional)
        self.assertIn("stack", demoted.reason)
        ok = dg.confirm_tier(_provisional("full"), _caps(), self.occ,
                             fcs=self.fcs, hw_stack_depth=16)
        self.assertFalse(ok.provisional)

    def test_symbols_from_var(self):
        syms = dg.symbols_from_var(self.var)
        self.assertEqual(syms["cdl_seq"]["address"], 0x023)
        self.assertEqual(syms["cdl_ring"]["size"], 16)
        self.assertEqual(syms["STATUS"]["address"], 0x003)
        self.assertEqual(syms["Carry"]["bit"], 0)         # bitfield keeps its bit index

    def test_budget_from_occ(self):
        b = dg.budget_from_occ(self.occ)
        self.assertTrue(b["measured"])
        self.assertEqual((b["ram_total"], b["code_words"]), (512, 18))

    def test_apply_measurement_fills_map(self):
        # A map fresh from build_map: empty symbols, unmeasured budget, provisional tier.
        map_payload = {"tier": "full", "tier_wire": 3, "tier_provisional": True,
                       "tier_forced": False, "tier_reason": "auto", "symbols": {},
                       "budget": {"measured": False}}
        decision = dg.confirm_tier(_provisional("full"), _caps(), self.occ)
        dg.apply_measurement(map_payload, decision, self.occ, self.var)
        self.assertFalse(map_payload["tier_provisional"])
        self.assertEqual(map_payload["budget"]["measured"], True)
        self.assertIn("cdl_seq", map_payload["symbols"])
        self.assertIn("measured", map_payload["tier_reason"])


if __name__ == "__main__":
    unittest.main()
