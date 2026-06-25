"""test_cdl_monitor.py -- decode/render tests for the debug-monitor core (P4).

Exercises the whole capture -> named-trace path with canned bytes (no hardware,
no packs): the two-layer RELAY/inner decode, the map application (channel names,
TRACE value width, target tick, symbol tagging), error recovery, and the
host->target command encoder with symbol resolution. This is the P4 acceptance
gate -- "decodes canned frames + renders named trace in CI".
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import cc5x_setcc_native as cli
from cc5x_setcc_native_lib import cdl_codec as codec
from cc5x_setcc_native_lib import cdl_monitor as mon


def make_map(**over) -> mon.MonitorMap:
    data = {
        "device": "PIC16F1509",
        "tier": "full",
        "capabilities": {"target_tick": False, "mem_read": True},
        "channels": [
            {"name": "adc", "id": 0, "width": 2},
            {"name": "state", "id": 1, "width": 1},
        ],
        "symbols": {"myVar": {"address": 0x20, "bank": 0, "size": 4, "class": "gpr"}},
    }
    data.update(over)
    return mon.MonitorMap(data)


def relay(seq: int, ts: int, *inner_frames: bytes) -> bytes:
    """Wrap concatenated inner target frame bytes in one 0xF0 RELAY envelope."""
    return codec.encode("RELAY", seq, tstamp=ts, data=b"".join(inner_frames))


class TwoLayerDecode(unittest.TestCase):
    def test_named_trace_through_relay(self):
        m = mon.Monitor(make_map())
        inner = codec.encode("TRACE", 1, bind={"value": 2}, ch=0, value=0x1234)
        events = list(m.feed(relay(0, 1000, inner)))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kind, "trace")
        self.assertEqual(ev.tstamp, 1000)
        self.assertEqual(ev.fields["value"], 0x1234)
        self.assertEqual(ev.fields["ch_name"], "adc")
        self.assertIn("adc = 0x1234", ev.text)
        self.assertIn("1000 us", ev.text)

    def test_frame_split_across_two_relay_envelopes(self):
        m = mon.Monitor(make_map())
        inner = codec.encode("TRACE", 2, bind={"value": 1}, ch=1, value=0x7E)
        # Split mid-inner-frame (mid-escape: value 0x7E stuffs to 7D 5E). Each half
        # rides its own RELAY envelope; the persistent inner deframer reassembles.
        cut = len(inner) // 2
        stream = relay(0, 10, inner[:cut]) + relay(1, 20, inner[cut:])
        events = list(m.feed(stream))
        self.assertEqual([e.kind for e in events], ["trace"])
        ev = events[0]
        self.assertEqual(ev.fields["ch_name"], "state")
        self.assertEqual(ev.fields["value"], 0x7E)
        # Stamped with the envelope that delivered the frame's closing FLAG.
        self.assertEqual(ev.tstamp, 20)

    def test_two_frames_one_envelope(self):
        m = mon.Monitor(make_map())
        f1 = codec.encode("TRACE", 1, bind={"value": 2}, ch=0, value=0x0001)
        f2 = codec.encode("LOG", 2, text=b"hi")
        events = list(m.feed(relay(0, 5, f1, f2)))
        self.assertEqual([e.kind for e in events], ["trace", "log"])
        self.assertIn("LOG: hi", events[1].text)

    def test_status_envelope(self):
        m = mon.Monitor(make_map())
        events = list(m.feed(codec.encode("STATUS", 0, dropped=5)))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "status")
        self.assertEqual(events[0].fields["dropped"], 5)
        self.assertIn("5 byte(s) dropped", events[0].text)


class DirectStream(unittest.TestCase):
    """A capture that is not probe-enveloped (raw target frames)."""

    def test_direct_hello_no_timestamp(self):
        m = mon.Monitor(make_map())
        hello = codec.encode("HELLO", 0, ver=1, devid=0x1609,
                             caps=proto_caps(), tier=1, ch_count=2)
        events = list(m.feed(hello))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kind, "hello")
        self.assertIsNone(ev.tstamp)
        self.assertIn("devid=0x1609", ev.text)
        self.assertIn("mem_read", ev.text)


def proto_caps() -> int:
    from cc5x_setcc_native_lib import cdl_proto as p
    return p.CAP_MEM_READ | p.CAP_SW_BREAKPOINTS


class MapApplication(unittest.TestCase):
    def test_mem_data_symbol_tagged(self):
        m = mon.Monitor(make_map())
        inner = codec.encode("MEM_DATA", 3, addr=0x20, bytes=bytes.fromhex("deadbeef"))
        ev = list(m.feed(relay(0, 1, inner)))[0]
        self.assertEqual(ev.kind, "mem_data")
        self.assertIn("(myVar)", ev.text)
        self.assertIn("de ad be ef", ev.text)

    def test_trace_with_target_tick(self):
        m = mon.Monitor(make_map(capabilities={"target_tick": True}))
        inner = codec.encode("TRACE", 1, bind={"tick": True, "value": 1},
                             ch=1, tick=0x0102, value=0x09)
        ev = list(m.feed(relay(0, 7, inner)))[0]
        self.assertEqual(ev.fields["value"], 0x09)
        self.assertEqual(ev.fields["tick"], 0x0102)
        self.assertIn("tick=", ev.text)

    def test_trace_unknown_channel_still_decodes_value_unnamed(self):
        # Value is read from the wire, so an absent channel decodes fine (no name).
        m = mon.Monitor(make_map())
        inner = codec.encode("TRACE", 1, bind={"value": 2}, ch=9, value=0x0042)
        ev = list(m.feed(relay(0, 1, inner)))[0]
        self.assertEqual(ev.kind, "trace")
        self.assertEqual(ev.fields["value"], 0x0042)
        self.assertNotIn("ch_name", ev.fields)
        self.assertIn("ch9", ev.text)

    def test_no_map_decodes_value_by_wire_bytes(self):
        m = mon.Monitor(None)
        inner = codec.encode("TRACE", 1, bind={"value": 2}, ch=0, value=0x1234)
        ev = list(m.feed(relay(0, 1, inner)))[0]
        self.assertEqual(ev.kind, "trace")
        self.assertEqual(ev.fields["value"], 0x1234)

    def test_stub_uns16_value_with_default_map_width_1(self):
        # Regression (Codex P1): the v0.1 stub always emits a 2-byte value, while a
        # channel's map width defaults to 1. The monitor must decode by the wire
        # bytes (value 0x1234), not the map width, and note the discrepancy -- not
        # report every real trace frame as undecoded.
        m = mon.Monitor(make_map(channels=[{"name": "state", "id": 0, "width": 1}]))
        inner = codec.encode("TRACE", 1, bind={"value": 2}, ch=0, value=0x1234)
        ev = list(m.feed(relay(0, 5, inner)))[0]
        self.assertEqual(ev.kind, "trace")
        self.assertEqual(ev.fields["value"], 0x1234)
        self.assertEqual(ev.fields["ch_name"], "state")
        self.assertIn("width_note", ev.fields)
        self.assertNotIn("decode_error", ev.fields)
        self.assertIn("state = 0x1234", ev.text)


class Recovery(unittest.TestCase):
    def test_inner_crc_error_then_good_frame(self):
        m = mon.Monitor(make_map())
        good = codec.encode("LOG", 1, text=b"ok")
        bad = bytearray(codec.encode("LOG", 2, text=b"xx"))
        bad[-2] ^= 0xFF  # corrupt the CRC byte (pre-FLAG)
        events = list(m.feed(relay(0, 1, bytes(bad), good)))
        kinds = [e.kind for e in events]
        self.assertIn("error", kinds)
        self.assertIn("log", kinds)
        self.assertIn("ok", events[-1].text)


class Commands(unittest.TestCase):
    def test_read_mem_resolves_symbol(self):
        m = make_map()
        frame = mon.encode_command("READ_MEM", 4, m, addr="myVar", len=4)
        decoded = list(codec.Deframer().feed(frame))[0]
        self.assertEqual(decoded["name"], "READ_MEM")
        self.assertEqual(decoded["addr"], 0x20)
        self.assertEqual(decoded["len"], 4)

    def test_read_mem_numeric_addr_passthrough(self):
        frame = mon.encode_command("READ_MEM", 0, None, addr=0x40, len=1)
        decoded = list(codec.Deframer().feed(frame))[0]
        self.assertEqual(decoded["addr"], 0x40)

    def test_set_bp_and_continue(self):
        for name in ("SET_BP", "CLR_BP", "CONTINUE"):
            frame = mon.encode_command(name, 0, None, bp_id=5)
            decoded = list(codec.Deframer().feed(frame))[0]
            self.assertEqual(decoded["name"], name)
            self.assertEqual(decoded["bp_id"], 5)

    def test_reject_non_host_command(self):
        with self.assertRaises(ValueError):
            mon.encode_command("TRACE", 0, None, ch=0, value=1)

    def test_symbol_without_map_raises(self):
        with self.assertRaises(ValueError):
            mon.encode_command("READ_MEM", 0, None, addr="myVar", len=1)

    def test_unknown_symbol_raises_valueerror_not_keyerror(self):
        # Contract: every bad-input path of encode_command raises ValueError, so a
        # caller catching ValueError handles them all uniformly.
        with self.assertRaises(ValueError):
            mon.encode_command("READ_MEM", 0, make_map(), addr="nope", len=1)


class MapValidation(unittest.TestCase):
    """A file that parses as JSON but is the wrong shape must raise ValueError so the
    CLI turns it into a clean error, never a raw KeyError/AttributeError traceback."""

    def test_non_object_map_raises_valueerror(self):
        with self.assertRaises(ValueError):
            mon.MonitorMap.from_json("[1, 2, 3]")

    def test_channel_missing_width_raises_valueerror(self):
        with self.assertRaises(ValueError):
            mon.MonitorMap({"channels": [{"name": "adc", "id": 0}]})

    def test_symbols_wrong_type_raises_valueerror(self):
        with self.assertRaises(ValueError):
            mon.MonitorMap({"symbols": [1, 2]})

    def test_symbol_entry_not_dict_is_tolerated(self):
        # A symbol whose value is not a dict simply has no resolvable address.
        m = mon.MonitorMap({"symbols": {"weird": 5}})
        self.assertIsNone(m.symbol_at(5))


class BpHitLayout(unittest.TestCase):
    def _decode(self, args: bytes) -> dict:
        frame = {"name": "BP_HIT", "args": args}
        mon._augment_bp_hit(frame)
        return frame

    def test_bp_id_only(self):
        f = self._decode(b"\x05")
        self.assertEqual(f["bp_id"], 5)
        self.assertNotIn("pc", f)
        self.assertNotIn("decode_error", f)

    def test_bp_id_pc(self):
        f = self._decode(b"\x05\x1c\x80")
        self.assertEqual(f["pc"], 0x801C)
        self.assertNotIn("nframe", f)

    def test_bp_id_pc_nframe(self):
        f = self._decode(b"\x05\x1c\x80\x03")
        self.assertEqual(f["pc"], 0x801C)
        self.assertEqual(f["nframe"], 3)

    def test_ambiguous_length_is_flagged_not_dropped(self):
        f = self._decode(b"\x05\x09")  # len 2: cannot disambiguate -> flagged
        self.assertEqual(f["bp_id"], 5)
        self.assertIn("decode_error", f)
        self.assertNotIn("nframe", f)


class Serialization(unittest.TestCase):
    def test_event_as_dict_is_json_friendly(self):
        m = mon.Monitor(make_map())
        inner = codec.encode("MEM_DATA", 3, addr=0x20, bytes=b"\xde\xad")
        ev = list(m.feed(relay(0, 99, inner)))[0]
        d = ev.as_dict()
        self.assertEqual(d["kind"], "mem_data")
        self.assertEqual(d["tstamp"], 99)
        self.assertEqual(d["fields"]["bytes"], "dead")  # bytes -> hex


def _sample_capture() -> bytes:
    hello = codec.encode("HELLO", 0, ver=1, devid=0x1609, caps=proto_caps(),
                         tier=1, ch_count=2)
    t0 = codec.encode("TRACE", 1, bind={"value": 2}, ch=0, value=0x1234)
    md = codec.encode("MEM_DATA", 2, addr=0x20, bytes=bytes.fromhex("deadbeef"))
    return relay(0, 100, hello) + relay(1, 250, t0) + codec.encode("STATUS", 0, dropped=3) \
        + relay(2, 400, md)


class CommandLine(unittest.TestCase):
    """The P4 acceptance gate: the `debug-monitor` command decodes a canned capture
    file and renders named trace -- end to end, no hardware."""

    def _run(self, argv: list[str]) -> tuple[int, str]:
        parser = cli.build_parser()
        args = parser.parse_args(argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_decode_capture_file_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "map.json").write_text(json.dumps({
                "device": "PIC16F1509", "tier": "full",
                "capabilities": {"target_tick": False, "mem_read": True},
                "channels": [{"name": "adc", "id": 0, "width": 2}],
                "symbols": {"myVar": {"address": 0x20}},
            }))
            (d / "cap.bin").write_bytes(_sample_capture())
            rc, out = self._run(["debug-monitor", "--map", str(d / "map.json"),
                                 "--input", str(d / "cap.bin"), "--json"])
            self.assertEqual(rc, 0)
            events = [json.loads(line) for line in out.splitlines() if line.strip()]
            kinds = [e["kind"] for e in events]
            self.assertEqual(kinds, ["hello", "trace", "status", "mem_data"])
            trace = events[1]
            self.assertEqual(trace["fields"]["ch_name"], "adc")
            self.assertEqual(trace["tstamp"], 250)
            self.assertIn("(myVar)", events[3]["text"])

    def test_decode_capture_text_named_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "map.json").write_text(json.dumps({
                "capabilities": {"target_tick": False},
                "channels": [{"name": "adc", "id": 0, "width": 2}], "symbols": {},
            }))
            (d / "cap.bin").write_bytes(relay(1, 250, codec.encode(
                "TRACE", 1, bind={"value": 2}, ch=0, value=0x1234)))
            rc, out = self._run(["debug-monitor", "--map", str(d / "map.json"),
                                 "--input", str(d / "cap.bin")])
            self.assertEqual(rc, 0)
            self.assertIn("TRACE adc = 0x1234", out)

    def test_hex_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            cap = relay(0, 5, codec.encode("LOG", 1, text=b"hi"))
            (d / "cap.hex").write_text(cap.hex(" ") + "\n")
            rc, out = self._run(["debug-monitor", "--input", str(d / "cap.hex"), "--hex"])
            self.assertEqual(rc, 0)
            self.assertIn("LOG: hi", out)

    def test_no_map_runs_and_renders_by_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "cap.bin").write_bytes(relay(0, 1, codec.encode("LOG", 1, text=b"x")))
            rc, out = self._run(["debug-monitor", "--input", str(d / "cap.bin")])
            self.assertEqual(rc, 0)
            self.assertIn("LOG: x", out)

    def test_missing_input_file_emits_json_error(self):
        rc, out = self._run(["debug-monitor", "--input", "/no/such/cap.bin", "--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "input_read_failed")

    def test_bad_map_emits_json_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "map.json"
            bad.write_text("{ not json")
            rc, out = self._run(["debug-monitor", "--map", str(bad),
                                 "--input", "/dev/null", "--json"])
            self.assertEqual(rc, 1)
            payload = json.loads(out)
            self.assertEqual(payload["error"]["kind"], "map_load_failed")


if __name__ == "__main__":
    unittest.main()
