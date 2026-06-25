from __future__ import annotations

import json
import unittest

from cc5x_setcc_native_lib import debuggen
from cc5x_setcc_native_lib.picmeta import (
    DeviceMetadata,
    IniSfr,
    IniSfrField,
    MemoryRange,
)


def make_metadata(
    *,
    device="PIC16F15244",
    arch="PIC14EX",
    procid="A305",  # pack PROCID is bare hex (no 0x prefix), like the real metadata
    rom_size_words=4096,
    bank_size=128,
    ram_ranges=None,
    common_ranges=None,
    with_eusart=True,
    with_rx=True,
    with_timer=True,
) -> DeviceMetadata:
    """Build a synthetic device model exercising the EUSART/RAM detection paths."""
    sfrs: list[IniSfr] = []
    fields: list[IniSfrField] = []

    def sfr(name, addr):
        sfrs.append(IniSfr(name=name, address=addr, width=8))

    def bit(name, addr, pos):
        fields.append(IniSfrField(name=name, address=addr, bit_position=pos, width=1))

    # Ports/TRIS used by pin wiring.
    for letter, base in (("A", 0x0C), ("B", 0x0D), ("C", 0x0E)):
        sfr(f"PORT{letter}", base)
        sfr(f"TRIS{letter}", base + 0x80)
        sfr(f"LAT{letter}", base + 0x100)
    sfr("PIR1", 0x11)
    bit("RCIF", 0x11, 5)
    if with_eusart:
        sfr("TX1STA", 0x19)
        sfr("RC1STA", 0x1A)
        sfr("TX1REG", 0x1B)
        if with_rx:
            sfr("RC1REG", 0x1C)
        sfr("SP1BRGL", 0x1D)
        bit("TRMT", 0x19, 1)
        bit("TXEN", 0x19, 5)
        bit("SYNC", 0x19, 4)
        bit("BRGH", 0x19, 2)
        bit("SPEN", 0x1A, 7)
        bit("CREN", 0x1A, 4)
    if with_timer:
        sfr("TMR1L", 0x15)
        sfr("TMR1H", 0x16)

    if ram_ranges is None:
        # 512 B of GPR across banks, plus a 16-byte common block mirrored into 3 banks.
        ram_ranges = [
            MemoryRange(0x20, 0x7F),
            MemoryRange(0xA0, 0xEF),
            MemoryRange(0x120, 0x16F),
            MemoryRange(0x1A0, 0x1EF),
            MemoryRange(0x220, 0x26F),
            MemoryRange(0x2A0, 0x2EF),
            MemoryRange(0x320, 0x32F),
        ]
    if common_ranges is None:
        common_ranges = [
            MemoryRange(0x70, 0x7F),
            MemoryRange(0xF0, 0xFF),
            MemoryRange(0x170, 0x17F),
        ]

    return DeviceMetadata(
        device=device,
        ini_arch=arch,
        ini_procid=procid,
        rom_size_words=rom_size_words,
        banks=64,
        bank_size=bank_size,
        sfr_count=len(sfrs),
        sfr_field_count=len(fields),
        config_word_count=0,
        config_setting_count=0,
        config_value_count=0,
        config_words=[],
        sfrs=sfrs,
        sfr_fields=fields,
        ram_ranges=ram_ranges,
        common_ranges=common_ranges,
        icd_ram_ranges=[],
        pic_summary=None,
    )


class ParseConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = debuggen.parse_debug_config(None)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.tier, "auto")
        self.assertFalse(cfg.write_mem)  # write off by default (01 §6)
        self.assertEqual(cfg.breakpoints, "software")
        self.assertEqual(cfg.channels, ())

    def test_channels_get_sequential_ids(self):
        cfg = debuggen.parse_debug_config(
            {"channels": [{"name": "state"}, {"name": "adc", "width": 2}]}
        )
        self.assertEqual([(c.name, c.id, c.width) for c in cfg.channels],
                         [("state", 0, 1), ("adc", 1, 2)])

    def test_duplicate_channel_rejected(self):
        with self.assertRaises(ValueError):
            debuggen.parse_debug_config({"channels": [{"name": "x"}, {"name": "x"}]})

    def test_wrong_types_rejected(self):
        with self.assertRaises(ValueError):
            debuggen.parse_debug_config({"enabled": "yes"})
        with self.assertRaises(ValueError):
            debuggen.parse_debug_config({"transport": {"brg": "0x19"}})
        with self.assertRaises(ValueError):
            debuggen.parse_debug_config([])


class DetectCapsTests(unittest.TestCase):
    def test_eusart_registers_resolved_from_metadata(self):
        caps = debuggen.detect_caps(make_metadata())
        self.assertTrue(caps.has_eusart)
        eu = caps.eusart
        self.assertEqual(eu.tx_data, "TX1REG")
        self.assertEqual(eu.rcif_reg, "PIR1")
        self.assertEqual(eu.rcif_bit, 5)
        self.assertEqual(eu.trmt_reg, "TX1STA")
        self.assertEqual(eu.spen_bit, 7)

    def test_ram_dedups_mirrored_common(self):
        # Regression: COMMON is the same block mirrored into every bank; it must not be
        # summed per-bank. 512 B GPR (the common block already lies inside bank 0).
        caps = debuggen.detect_caps(make_metadata())
        self.assertEqual(caps.ram_bytes, 512)

    def test_common_counted_once_when_disjoint(self):
        meta = make_metadata(
            ram_ranges=[MemoryRange(0x20, 0x6F)],  # 80 B GPR, no common inside
            common_ranges=[MemoryRange(0x70, 0x7F), MemoryRange(0xF0, 0xFF)],  # 16 B mirrored
        )
        self.assertEqual(debuggen.detect_caps(meta).ram_bytes, 80 + 16)

    def test_no_eusart(self):
        caps = debuggen.detect_caps(make_metadata(with_eusart=False))
        self.assertFalse(caps.has_eusart)
        self.assertIsNone(caps.eusart)


class DeviceIdTests(unittest.TestCase):
    def test_bare_hex_procid(self):
        # Regression: pack PROCID is bare hex; base-16 parse, not base-0.
        self.assertEqual(debuggen._device_id(make_metadata(procid="A305")), 0xA305)
        self.assertEqual(debuggen._device_id(make_metadata(procid="1509")), 0x1509)

    def test_0x_prefixed_still_ok(self):
        self.assertEqual(debuggen._device_id(make_metadata(procid="0xA305")), 0xA305)

    def test_missing_procid(self):
        self.assertEqual(debuggen._device_id(make_metadata(procid=None)), 0)


class TierSelectionTests(unittest.TestCase):
    def _decide(self, tier="auto", **meta_kw):
        caps = debuggen.detect_caps(make_metadata(**meta_kw))
        cfg = debuggen.parse_debug_config({"tier": tier})
        return debuggen.select_tier(cfg, caps)

    def test_full_when_roomy(self):
        self.assertEqual(self._decide(rom_size_words=4096).tier, "full")

    def test_min_when_flash_below_full_floor(self):
        self.assertEqual(self._decide(rom_size_words=3584).tier, "min")

    def test_trace_when_no_eusart(self):
        self.assertEqual(self._decide(with_eusart=False, rom_size_words=2048).tier, "trace")

    def test_toggle_when_tiny(self):
        d = self._decide(
            with_eusart=False,
            rom_size_words=256,
            ram_ranges=[MemoryRange(0x20, 0x2F)],
            common_ranges=[],
        )
        self.assertEqual(d.tier, "toggle")

    def test_force_lower_allowed(self):
        self.assertEqual(self._decide(tier="trace").tier, "trace")

    def test_force_higher_rejected(self):
        with self.assertRaises(ValueError):
            self._decide(tier="full", with_eusart=False, rom_size_words=2048)


class ValidateTests(unittest.TestCase):
    def _errors(self, payload, **meta_kw):
        meta = make_metadata(**meta_kw)
        cfg = debuggen.parse_debug_config(payload)
        caps = debuggen.detect_caps(meta)
        decision = debuggen.select_tier(cfg, caps)
        return debuggen.validate_debug(cfg, caps, decision)

    def test_monitor_requires_brg(self):
        errs = self._errors({"tier": "full"})
        self.assertTrue(any("brg" in e for e in errs))

    def test_brg_satisfies(self):
        self.assertEqual(self._errors({"tier": "full", "transport": {"brg": 25}}), [])

    def test_write_mem_requires_monitor(self):
        errs = self._errors(
            {"tier": "trace", "write_mem": True}, with_eusart=False, rom_size_words=2048
        )
        self.assertTrue(any("write_mem" in e for e in errs))

    def test_trace_without_eusart_rejected(self):
        # Auto-selected 'trace' on a no-EUSART part is not buildable yet (bit-bang TODO).
        errs = self._errors({}, with_eusart=False, rom_size_words=2048)
        self.assertTrue(any("bit-bang" in e for e in errs))

    def test_monitor_requires_enhanced_arch(self):
        errs = self._errors({"tier": "full", "transport": {"brg": 25}}, arch="PIC14")
        self.assertTrue(any("enhanced-midrange" in e for e in errs))

    def test_target_tick_not_advertised(self):
        # Reserved/unimplemented: requesting it must NOT set the cap bit.
        meta = make_metadata()
        cfg = debuggen.parse_debug_config(
            {"tier": "full", "transport": {"brg": 25}, "target_timestamp": True}
        )
        caps = debuggen.detect_caps(meta)
        decision = debuggen.select_tier(cfg, caps)
        self.assertEqual(debuggen.validate_debug(cfg, caps, decision), [])
        bits = debuggen._capabilities(cfg, caps, decision)
        self.assertEqual(bits & debuggen.CAP_TARGET_TICK, 0)


class MapTests(unittest.TestCase):
    def _map(self, payload):
        meta = make_metadata()
        return debuggen.generate_debug_stub(meta, payload)

    def test_capabilities_and_channels(self):
        gen = self._map(
            {"tier": "full", "transport": {"brg": 25}, "channels": [{"name": "state"}],
             "write_mem": True}
        )
        obj = json.loads(gen.map_json)
        self.assertEqual(obj["tier"], "full")
        self.assertTrue(obj["tier_provisional"])
        self.assertTrue(obj["capabilities"]["mem_read"])
        self.assertTrue(obj["capabilities"]["mem_write"])
        self.assertTrue(obj["capabilities"]["sw_breakpoints"])
        self.assertEqual(obj["channels"], [{"name": "state", "id": 0, "width": 1}])
        self.assertEqual(obj["symbols"], {})
        self.assertFalse(obj["budget"]["measured"])
        self.assertEqual(obj["protocol"]["crc_poly"], debuggen.CDL_CRC_POLY)

    def test_write_off_by_default(self):
        gen = self._map({"tier": "full", "transport": {"brg": 25}})
        self.assertFalse(json.loads(gen.map_json)["capabilities"]["mem_write"])


class StubEmissionTests(unittest.TestCase):
    def _gen(self, payload=None):
        if payload is None:
            payload = {"tier": "full", "transport": {"tx_pin": "RB7", "rx_pin": "RB5", "brg": 25},
                       "channels": [{"name": "state"}, {"name": "adc", "width": 2}]}
        return debuggen.generate_debug_stub(make_metadata(), payload)

    def test_filenames(self):
        gen = self._gen()
        self.assertEqual(gen.monitor_h_name, "cdl_monitor_16F15244.h")
        self.assertEqual(gen.monitor_c_name, "cdl_monitor_16F15244.c")
        self.assertEqual(gen.map_name, "cdl_map_16F15244.json")

    def test_header_has_guard_channels_and_macros(self):
        h = self._gen().monitor_h
        self.assertIn("#ifndef CDL_MONITOR_PIC16F15244_H", h)
        self.assertIn("#define CDL_CH_state", h)
        self.assertIn("#define CDL_TRACE(name, value)", h)
        self.assertIn("#define CDL_BP(id) cdl_bp(id)", h)

    def test_source_has_init_and_resolved_registers(self):
        c = self._gen().monitor_c
        self.assertIn("void cdl_init(void)", c)
        self.assertIn("TX1REG = b;", c)          # resolved from metadata, not hardcoded
        self.assertIn("SP1BRGL = CDL_SPBRG_VALUE;", c)

    def test_no_cc5x_unsafe_constructs(self):
        # Regression for the CC5X dialect fixes: no ~(1<<n) integer-overflow masks and no
        # variable-count shifts -- both fail real CC5X codegen.
        c = self._gen().monitor_c
        self.assertNotIn("~(1 <<", c)
        self.assertNotIn("<< id)", c)
        self.assertNotIn("<< cdl_rx", c)
        self.assertIn("cdl_bitmask(", c)  # the loop-based replacement is used instead

    def test_brg_error_when_missing_but_forced(self):
        # A monitor tier with no brg is rejected by validate_debug before emission.
        with self.assertRaises(ValueError):
            debuggen.generate_debug_stub(make_metadata(), {"tier": "full"})

    def test_command_length_guards_emitted(self):
        # READ_MEM/SET_BP/etc must NAK BAD_LEN on a too-short frame, not read stale bytes.
        c = self._gen().monitor_c
        self.assertIn("if (len < 3) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }", c)
        self.assertIn("CDL_NAK_BAD_LEN", c)

    def test_ack_payload_is_ref_seq_only(self):
        # ACK ARG is ref_seq only (cdl_proto ACK); NAK carries ref_seq + code.
        # A 2-byte ACK is rejected by the reference decoder (trailing byte).
        c = self._gen().monitor_c
        self.assertIn("static void cdl_ack(uns8 refseq) {", c)
        self.assertIn("cdl_send(CDL_T_ACK, a, 1);", c)
        self.assertIn("static void cdl_nak(uns8 refseq, uns8 code) {", c)
        self.assertIn("cdl_send(CDL_T_NAK, a, 2);", c)
        self.assertIn("cdl_ack(seq);", c)              # PING / SET_BP / CONTINUE

    def test_cren_enabled_when_rx_advertised_without_rx_pin(self):
        # A full-tier stub advertises CAP_RX_COMMANDS and emits a real cdl_poll()
        # whenever the receive path exists, even with no explicit rx_pin. CREN must
        # follow that (RCSTA = SPEN|CREN), else inbound commands never arrive.
        c = self._gen({"tier": "full", "transport": {"tx_pin": "RB7", "brg": 25},
                       "channels": [{"name": "state"}]}).monitor_c
        self.assertIn("RC1STA = 0x90;", c)             # SPEN(0x80) | CREN(0x10)
        self.assertIn("while (PIR1 & 0x20) cdl_rx_byte(RC1REG);", c)  # real poll, not stub

    def test_rx_caps_gated_on_receive_path(self):
        # Full tier on an EUSART with TX but no RCREG/RCIF advertises no host->target
        # commands -- the stub emits only the empty poll stub and could never dispatch.
        g = debuggen.generate_debug_stub(
            make_metadata(with_rx=False),
            {"tier": "full", "transport": {"tx_pin": "RB7", "brg": 25}, "channels": [{"name": "s"}]},
        )
        caps = json.loads(g.map_json)["capabilities"]
        self.assertEqual(caps["bits"], 0)
        self.assertFalse(caps["mem_read"])
        self.assertFalse(caps["mem_write"])
        self.assertIn("// No EUSART RX register", g.monitor_c)  # empty poll stub, not the receiver

    def test_toggle_fixed_site_emits_all_pins(self):
        # Regression: multi-site fixed-site toggle must pulse a distinct pin per site,
        # selected by marker id -- not always toggle_pins[0].
        g = debuggen.generate_debug_stub(
            make_metadata(),
            {"tier": "toggle",
             "toggle": {"encoding": "fixed-site", "pins": ["RB7", "RB5", "RA0"]},
             "channels": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
        )
        c = g.monitor_c
        self.assertIn("LATB = LATB | 0x80;", c)   # RB7 site 0
        self.assertIn("LATB = LATB | 0x20;", c)   # RB5 site 1
        self.assertIn("LATA = LATA | 0x01;", c)   # RA0 site 2
        self.assertIn("if (id == 0)", c)
        self.assertIn("} else if (id == 1)", c)
        self.assertIn("} else if (id == 2)", c)

    def test_toggle_single_pin_stays_branchless(self):
        g = debuggen.generate_debug_stub(
            make_metadata(),
            {"tier": "toggle", "toggle": {"encoding": "fixed-site", "pins": ["RA0"]},
             "channels": [{"name": "a"}]},
        )
        c = g.monitor_c
        self.assertNotIn("if (id ==", c)          # no dispatch ladder for a single site
        self.assertIn("a single pulse", c)

    def test_write_mem_dispatch_and_whitelist(self):
        # write_mem true emits a real WRITE_MEM path gated by a GPR-RAM whitelist that
        # NAKs WRITE_DENIED; SFR space (page-0 < 0x20) is never writable.
        g = debuggen.generate_debug_stub(
            make_metadata(),
            {"tier": "full", "transport": {"tx_pin": "RB7", "rx_pin": "RB5", "brg": 25},
             "write_mem": True, "channels": [{"name": "s"}]},
        )
        c = g.monitor_c
        self.assertIn("} else if (type == CDL_T_WRITE_MEM) {", c)
        self.assertIn("static void cdl_write_mem(uns8 refseq, uns8 lo, uns8 hi, uns8 n) {", c)
        self.assertIn("static uns8 cdl_addr_writable(uns8 lo, uns8 hi) {", c)
        self.assertIn("cdl_nak(refseq, CDL_NAK_WRITE_DENIED)", c)
        self.assertIn("lo >= 0x20", c)            # GPR starts at 0x20 on page 0; SFRs excluded
        self.assertTrue(json.loads(g.map_json)["capabilities"]["mem_write"])

    def test_write_mem_absent_by_default(self):
        c = self._gen().monitor_c  # default payload leaves write_mem off
        self.assertNotIn("CDL_T_WRITE_MEM", c)
        self.assertNotIn("cdl_write_mem", c)
        self.assertNotIn("cdl_addr_writable", c)

    def test_writable_ranges_excludes_sfrs(self):
        ranges = debuggen._writable_ranges(make_metadata())
        flat: set[int] = set()
        for page, lo, hi_lo in ranges:
            self.assertLessEqual(lo, hi_lo)
            flat.update((page << 8) | b for b in range(lo, hi_lo + 1))
        self.assertNotIn(0x000, flat)             # core SFRs
        self.assertNotIn(0x01F, flat)
        self.assertIn(0x020, flat)                # first GPR

    def test_device_id_in_header(self):
        h = self._gen().monitor_h
        self.assertIn("#define CDL_DEVID_HI      0xA3", h)
        self.assertIn("#define CDL_DEVID_LO      0x05", h)


if __name__ == "__main__":
    unittest.main()
