"""test_cdl_proto.py -- invariants + wire-value contract for the CDL protocol spec.

Locks cdl_proto.py to the bytes the wire format already ships: the spec in
cc5x-debug-probe/01-debug-link-protocol.md SS 4, the probe firmware's relay.c
(the 0xF0/0xF1 envelopes), and the constants debuggen currently emits. If the P1
single-source refactor (05 SS 5) ever changes a code or constant, these fail.
Pure data: no hardware, no packs.
"""
from __future__ import annotations

import unittest

from cc5x_setcc_native_lib import cdl_proto as p


class CatalogueInvariants(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(p.validate_catalogue(), [])

    def test_bytes_tail_is_last(self):
        for m in p.MESSAGES:
            for i, fld in enumerate(m.fields):
                if isinstance(fld.kind, p.Bytes):
                    self.assertEqual(i, len(m.fields) - 1, m.name)

    def test_group_ranges(self):
        for m in p.MESSAGES:
            lo, hi = p._GROUP_RANGE[m.group]
            self.assertTrue(lo <= m.type <= hi, m.name)

    def test_lookups_consistent(self):
        self.assertEqual(len(p.BY_TYPE), len(p.MESSAGES))   # no duplicate codes
        for m in p.MESSAGES:
            self.assertIs(p.BY_NAME[m.name], m)
            self.assertIs(p.BY_TYPE[m.type], m)


class WireValueContract(unittest.TestCase):
    """Lock the spec to bytes already shipped (01 SS 4; relay.c; debuggen)."""

    def test_framing_constants(self):
        self.assertEqual((p.CDL_FLAG, p.CDL_ESC, p.CDL_ESC_XOR), (0x7E, 0x7D, 0x20))
        # XOR stuffing must equal the probe firmware's literal pairs (01 SS 3).
        self.assertEqual(p.CDL_FLAG ^ p.CDL_ESC_XOR, 0x5E)
        self.assertEqual(p.CDL_ESC ^ p.CDL_ESC_XOR, 0x5D)
        self.assertEqual((p.CDL_CRC.poly, p.CDL_CRC.init, p.CDL_CRC.width), (0x07, 0x00, 8))
        self.assertEqual(p.CDL_PROTOCOL_VERSION, (0, 1))

    def test_type_codes(self):
        self.assertEqual(p.MSG_TYPES_T2H, {
            "HELLO": 0x01, "TRACE": 0x02, "LOG": 0x03, "MEM_DATA": 0x04,
            "BP_HIT": 0x05, "ACK": 0x06, "NAK": 0x07})
        self.assertEqual(p.MSG_TYPES_H2T, {
            "PING": 0x81, "READ_MEM": 0x82, "WRITE_MEM": 0x83, "SET_BP": 0x84,
            "CLR_BP": 0x85, "CONTINUE": 0x86, "SET_TRACE": 0x87})
        self.assertEqual(p.MSG_TYPES_PROBE, {"RELAY": 0xF0, "STATUS": 0xF1})

    def test_caps_and_nak(self):
        self.assertEqual(
            (p.CAP_MEM_READ, p.CAP_MEM_WRITE, p.CAP_SW_BREAKPOINTS,
             p.CAP_TARGET_TICK, p.CAP_RX_COMMANDS), (0x01, 0x02, 0x04, 0x08, 0x10))
        self.assertEqual(p.NAK_CODES, {"BAD_ADDR": 1, "SIDE_EFFECT": 2,
                                       "WRITE_DENIED": 3, "BAD_LEN": 4, "UNKNOWN_TYPE": 5,
                                       "BAD_BP": 6})

    def test_hello_layout(self):
        # 01 SS 4: ver, device-id(2), caps(1), tier(1), trace-ch-count(1) = 6 bytes,
        # matching debuggen's cdl_hello() arg order (debuggen.py _FRAMER_LINES).
        hello = p.BY_NAME["HELLO"]
        self.assertEqual([f.name for f in hello.fields],
                         ["ver", "devid", "caps", "tier", "ch_count"])
        self.assertEqual(hello.fields[1].kind, p.U16LE)

    def test_relay_envelope_matches_firmware(self):
        # firmware/probe-f042/src/relay.c: 0xF0 = tstamp(4 LE) + data; 0xF1 = dropped(4 LE).
        relay = p.BY_NAME["RELAY"]
        self.assertEqual(relay.type, 0xF0)
        self.assertEqual([f.name for f in relay.fields], ["tstamp", "data"])
        self.assertEqual(relay.fields[0].kind, p.U32LE)
        self.assertIsInstance(relay.fields[1].kind, p.Bytes)
        self.assertEqual(p.BY_NAME["STATUS"].type, 0xF1)


if __name__ == "__main__":
    unittest.main()
