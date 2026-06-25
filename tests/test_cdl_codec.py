"""test_cdl_codec.py -- round-trip, golden-vector, and recovery tests for cdl_codec.

The golden RELAY frame is byte-identical to the one asserted in the probe repo
(cc5x-debug-probe firmware/probe-f042/tests/test_relay.c and
tools/relay_decode.py), so the Python codec, the C encoder, and the Python
decoder are all pinned to one constant (05 SS 6, flow C). The HELLO/TRACE arg
bytes are checked against the worked example in 01-debug-link-protocol.md SS 10.
Pure Python: no hardware, no packs.
"""
from __future__ import annotations

import unittest

from cc5x_setcc_native_lib import cdl_codec as c


# The exact 15 bytes for RELAY seq=0, ts=256, data={7E,7D,42} -- the cross-repo
# anchor (matches cc5x-debug-probe test_relay.c test_golden_vector).
GOLDEN_RELAY = bytes.fromhex("7ef00007000100007d5e7d5d42e27e")


class GoldenVector(unittest.TestCase):
    def test_encode_matches_cross_repo_golden(self):
        frame = c.encode("RELAY", 0, tstamp=256, data=bytes([0x7E, 0x7D, 0x42]))
        self.assertEqual(frame, GOLDEN_RELAY)

    def test_deframer_decodes_golden(self):
        frames = list(c.Deframer().feed(GOLDEN_RELAY))
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f["name"], "RELAY")
        self.assertEqual(f["seq"], 0)
        self.assertEqual(f["tstamp"], 256)
        self.assertEqual(f["data"], bytes([0x7E, 0x7D, 0x42]))


class RoundTrip(unittest.TestCase):
    def _round(self, name, seq, *, bind=None, **fields):
        frame = c.encode(name, seq, bind=bind, **fields)
        out = list(c.Deframer().feed(frame))
        self.assertEqual(len(out), 1, out)
        return out[0]

    def test_fully_determined_messages(self):
        # RELAY / STATUS / HELLO / MEM_DATA / READ_MEM decode straight from feed().
        f = self._round("STATUS", 5, dropped=0x01020304)
        self.assertEqual((f["name"], f["seq"], f["dropped"]), ("STATUS", 5, 0x01020304))

        f = self._round("HELLO", 0, ver=1, devid=0x0916, caps=0x07, tier=1, ch_count=3)
        self.assertEqual(f["devid"], 0x0916)
        self.assertEqual(f["ch_count"], 3)

        f = self._round("MEM_DATA", 3, addr=0x0020, bytes=b"\xde\xad\xbe\xef")
        self.assertEqual((f["addr"], f["bytes"]), (0x0020, b"\xde\xad\xbe\xef"))

        f = self._round("READ_MEM", 1, addr=0x0020, len=4)
        self.assertEqual((f["addr"], f["len"]), (0x0020, 4))

    def test_variable_layout_needs_bind(self):
        # TRACE has an optional tick + a per-build value width -> feed() leaves raw
        # args; the caller decodes with the build's bind (05 SS 9).
        bind = {"tick": False, "value": 2}
        frame = c.encode("TRACE", 1, bind=bind, ch=0, value=0x112A)
        self.assertEqual(c.encode_args("TRACE", bind=bind, ch=0, value=0x112A),
                         bytes([0x00, 0x2A, 0x11]))          # 01 SS 10 worked example
        out = list(c.Deframer().feed(frame))[0]
        self.assertEqual(out["name"], "TRACE")
        self.assertNotIn("value", out)                       # not auto-decoded
        self.assertEqual(c.decode_args("TRACE", out["args"], bind=bind),
                         {"ch": 0, "value": 0x112A})


class SpecArgBytes(unittest.TestCase):
    def test_hello_args_match_worked_example(self):
        # 01 SS 10: HELLO args = 01 16 09 07 01 03 (ver, devid LE, caps, tier, chs).
        self.assertEqual(
            c.encode_args("HELLO", ver=1, devid=0x0916, caps=0x07, tier=1, ch_count=3),
            bytes.fromhex("011609070103"))

    def test_unexpected_or_missing_field_raises(self):
        with self.assertRaises(ValueError):
            c.encode("READ_MEM", 0, addr=0x10)               # missing 'len'
        with self.assertRaises(ValueError):
            c.encode("STATUS", 0, dropped=0, bogus=1)        # unexpected field


class Recovery(unittest.TestCase):
    """Ported from relay_decode.py selftest: errors don't desync the next frame."""

    def test_crc_error_then_resync(self):
        bad = bytearray(c.encode("STATUS", 9, dropped=0xDEAD))
        bad[-2] ^= 0xFF                                       # corrupt the CRC byte
        good = c.encode("STATUS", 10, dropped=0xBEEF)
        out = list(c.Deframer().feed(bytes(bad) + good))
        self.assertTrue(any("error" in f for f in out))
        self.assertTrue(any(f.get("seq") == 10 for f in out))

    def test_dangling_escape_then_resync(self):
        d = c.Deframer()
        out = list(d.feed(b"\x7e\xaa\xbb\x7d\x7e" + c.encode("STATUS", 11, dropped=1)))
        self.assertTrue(any(f.get("error") == "escape" for f in out))
        self.assertTrue(any(f.get("seq") == 11 for f in out))

    def test_double_escape_is_rejected_not_normalized(self):
        # A pending escape must consume the next byte even when it is itself ESC. The
        # old order (check b==ESC before the escape state) let `ESC ESC` re-arm the
        # escape, so inserting a stray ESC before a valid ESC ESC_FLAG pair decoded to
        # the *same* valid frame -- malformed wire silently accepted. Here the stray
        # ESC lands just before the golden frame's `7d 5e` pair (index 8).
        tampered = GOLDEN_RELAY[:8] + b"\x7d" + GOLDEN_RELAY[8:]
        out = list(c.Deframer().feed(tampered))
        self.assertEqual([f.get("error") for f in out], ["escape"])
        self.assertFalse(any(f.get("name") == "RELAY" for f in out))

    def test_escaped_esc_and_flag_still_decode(self):
        # The fix must not break legitimate stuffing: ESC ESC_ESC -> 0x7D and
        # ESC ESC_FLAG -> 0x7E as data bytes round-trip through the deframer.
        frame = c.encode("RELAY", 3, tstamp=0, data=bytes([0x7D, 0x7E, 0x41]))
        out = list(c.Deframer().feed(frame))
        self.assertEqual(out[0]["data"], bytes([0x7D, 0x7E, 0x41]))

    def test_unknown_type_passes_through(self):
        # A well-formed frame with an unallocated TYPE: name=None, raw args, no error.
        body = bytes([0x7A, 0x00, 0x01, 0xAB])               # TYPE 0x7A unallocated
        frame = bytearray([0x7E])
        for b in body:
            c._stuff(frame, b)
        c._stuff(frame, c.crc8(body))
        frame.append(0x7E)
        out = list(c.Deframer().feed(bytes(frame)))
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["name"])
        self.assertEqual(out[0]["type"], 0x7A)
        self.assertEqual(out[0]["args"], b"\xab")


if __name__ == "__main__":
    unittest.main()
