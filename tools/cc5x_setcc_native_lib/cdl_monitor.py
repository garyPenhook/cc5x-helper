"""cdl_monitor.py -- decode a CDL capture and render named, timestamped trace.

This is the decode/render core of the ``debug-monitor`` PC command (master-plan
phase P4). It imports the reference codec (cdl_codec, 05 SS 1/9) and applies a
``cdl_map_<dev>.json`` so a raw capture becomes human-readable: trace channels by
name, memory replies tagged with the symbol at the address, capabilities/tier
from HELLO.

Like cdl_codec, this module does **no serial I/O** -- it consumes ``bytes`` and
yields events. The CLI owns the file/stdin/serial source, so the whole decode +
render path is exercised by canned captures in CI (the P4 acceptance gate).

Two decode layers (01 SS 2/5, firmware/probe-f042/src/relay.c):
  - *Outer*: the probe wraps raw target bytes in ``0xF0 RELAY`` envelopes that
    each carry a 4-byte microsecond timestamp, plus ``0xF1 STATUS`` drop counts.
  - *Inner*: ``RELAY.data`` is the raw target UART stream -- itself FLAG-delimited
    CDL frames (HELLO/TRACE/LOG/MEM_DATA/...). A single target frame may split
    across several RELAY envelopes, so the inner deframer is persistent.

A capture that is *not* probe-enveloped (a direct target stream) is also handled:
target-origin frames seen at the outer layer are rendered directly, with no probe
timestamp.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import cdl_codec
from . import cdl_proto as proto


@dataclass(frozen=True)
class Channel:
    name: str
    id: int
    width: int


class MonitorMap:
    """The decode-relevant view of a ``cdl_map_<dev>.json`` (built by debuggen's
    ``build_map``). Carries channels (for TRACE), capabilities (whether TRACE
    frames include a target tick), and the symbol table (address <-> name)."""

    def __init__(self, data: dict) -> None:
        # A file that *parses* as JSON can still be the wrong shape; normalize every
        # bad-shape error to ValueError so the CLI's (OSError, ValueError) guard turns
        # it into a clean map_load_failed instead of a raw KeyError/AttributeError.
        if not isinstance(data, dict):
            raise ValueError("cdl map must be a JSON object")
        self.device: str | None = data.get("device")
        self.tier: str | None = data.get("tier")
        caps = data.get("capabilities")
        self.target_tick: bool = bool(caps.get("target_tick", False)) if isinstance(caps, dict) else False
        self.channels: dict[int, Channel] = {}
        for ch in data.get("channels") or []:
            try:
                chan = Channel(name=ch["name"], id=int(ch["id"]), width=int(ch["width"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"bad channel entry {ch!r}: {exc}") from exc
            self.channels[chan.id] = chan
        # symbols: name -> {address,bank,size,class,...}; keep the full entry and a
        # reverse address -> name index for tagging MEM_DATA replies (01 SS 6).
        symbols = data.get("symbols") or {}
        if not isinstance(symbols, dict):
            raise ValueError("cdl map 'symbols' must be an object")
        self.symbols: dict[str, dict] = dict(symbols)
        self._by_addr: dict[int, str] = {}
        for name, entry in self.symbols.items():
            addr = entry.get("address") if isinstance(entry, dict) else None
            if isinstance(addr, int):
                self._by_addr.setdefault(addr, name)

    @classmethod
    def from_json(cls, text: str) -> MonitorMap:
        return cls(json.loads(text))

    @classmethod
    def from_file(cls, path: str | Path) -> MonitorMap:
        # The map is ASCII JSON; debuggen writes it latin-1, which is a superset
        # for ASCII, so either codec round-trips it.
        return cls.from_json(Path(path).read_text(encoding="latin-1"))

    def symbol_at(self, addr: int) -> str | None:
        """Name of the symbol that *starts* at ``addr`` (exact match), or None."""
        return self._by_addr.get(addr)

    def resolve_symbol(self, name: str) -> int:
        """Map a symbol name to its flat file-register address (01 SS 6). Raises
        KeyError if unknown, ValueError if the entry has no address."""
        entry = self.symbols[name]
        addr = entry.get("address")
        if not isinstance(addr, int):
            raise ValueError(f"symbol {name!r} has no integer address in the map")
        return addr


@dataclass
class Event:
    """One decoded protocol event, ready to render or serialize.

    ``tstamp`` is the probe's microsecond stamp when the frame arrived inside a
    RELAY envelope, else None (direct/un-enveloped target frame, or a probe
    STATUS). ``kind`` is a stable lowercase tag for filtering; ``fields`` holds the
    decoded values; ``text`` is the one-line human rendering."""

    kind: str
    text: str
    tstamp: int | None = None
    seq: int | None = None
    name: str | None = None
    fields: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out: dict = {"kind": self.kind, "text": self.text}
        if self.tstamp is not None:
            out["tstamp"] = self.tstamp
        if self.seq is not None:
            out["seq"] = self.seq
        if self.name is not None:
            out["name"] = self.name
        if self.fields:
            out["fields"] = _jsonable(self.fields)
        return out


def _jsonable(obj: object) -> object:
    """Recursively convert bytes -> hex string so an event serializes as JSON."""
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).hex()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


class Monitor:
    """Stateful two-layer decoder. Feed capture ``bytes``; iterate ``Event``s.

    Holds an outer deframer (probe envelopes) and a *separate* persistent inner
    deframer (target stream reassembled from RELAY payloads), so a target frame
    split across envelopes is still recovered."""

    def __init__(self, mmap: MonitorMap | None = None) -> None:
        self.map = mmap
        self._outer = cdl_codec.Deframer()
        self._inner = cdl_codec.Deframer()

    def feed(self, chunk: bytes) -> Iterator[Event]:
        for frame in self._outer.feed(chunk):
            name = frame.get("name")
            if name == "RELAY":
                ts = frame.get("tstamp")
                # Reassemble + decode the inner target stream; stamp every frame
                # that completes during this envelope with the envelope's tick.
                for inner in self._inner.feed(frame.get("data", b"")):
                    yield self._event(inner, tstamp=ts)
            elif name == "STATUS":
                yield self._status_event(frame)
            else:
                # Not a probe envelope: a direct target frame, an unknown type, or
                # an outer-layer error. Render with no probe timestamp.
                yield self._event(frame, tstamp=None)

    # -- per-frame handling ------------------------------------------------------

    def _event(self, frame: dict, *, tstamp: int | None) -> Event:
        if "error" in frame:
            raw = frame.get("raw", "")
            detail = frame.get("detail")
            text = f"!! frame error: {frame['error']}" + (f" ({detail})" if detail else "")
            text += f" raw={raw}" if raw else ""
            return Event("error", text, tstamp=tstamp, fields=dict(frame))
        frame = self._augment(frame)
        name = frame.get("name")
        seq = frame.get("seq")
        if name is None:
            text = f"?? unknown type 0x{frame.get('type', 0):02X} seq={seq} " \
                   f"args={bytes(frame.get('args', b'')).hex()}"
            return Event("raw", _stamp(text, tstamp), tstamp=tstamp, seq=seq, name=None,
                         fields={"type": frame.get("type"), "args": frame.get("args", b"")})
        kind = name.lower()
        text = _stamp(self._render(frame), tstamp)
        return Event(kind, text, tstamp=tstamp, seq=seq, name=name,
                     fields=_extra_fields(frame))

    def _status_event(self, frame: dict) -> Event:
        dropped = frame.get("dropped", 0)
        return Event("status", f"-- probe STATUS: {dropped} byte(s) dropped",
                     tstamp=None, seq=frame.get("seq"), name="STATUS",
                     fields={"dropped": dropped})

    def _augment(self, frame: dict) -> dict:
        """Resolve the messages cdl_codec leaves raw because their ARG layout is
        per-build (TRACE width/tick, BP_HIT/NAK optionals -- 05 SS 9 / 01 SS 4)."""
        name = frame.get("name")
        if name == "TRACE" and "value" not in frame:
            self._augment_trace(frame)
        elif name == "BP_HIT" and "bp_id" not in frame:
            _augment_bp_hit(frame)
        elif name == "NAK" and "ref_seq" not in frame:
            _augment_nak(frame)
        return frame

    def _augment_trace(self, frame: dict) -> None:
        # The value is exactly the bytes left after ch (and the optional tick) -- LEN
        # delimits them -- so decode by what is on the wire, not the map's channel
        # width. The v0.1 stub hardcodes `cdl_trace(uns8 ch, uns16 value)` and always
        # emits a 2-byte value (debuggen.py cdl_trace), while a channel's map width
        # defaults to 1; trusting the map width would mis-decode every real frame.
        # Reading the wire bytes also matches the design's VarVal(1,4) (01 SS 4) and
        # still decodes a TRACE whose channel is absent from the map (just unnamed).
        args = bytes(frame.get("args", b""))
        if not args:
            frame["decode_error"] = "TRACE has no channel byte"
            return
        ch_id = args[0]
        frame["ch"] = ch_id
        off = 1
        if self.map is not None and self.map.target_tick:
            if len(args) < off + 2:
                frame["decode_error"] = "TRACE too short for the advertised target tick"
                return
            frame["tick"] = int.from_bytes(args[off:off + 2], "little")
            off += 2
        value_bytes = args[off:]
        if not 1 <= len(value_bytes) <= 4:
            frame["decode_error"] = f"TRACE value width {len(value_bytes)} outside 1..4"
            return
        frame["value"] = int.from_bytes(value_bytes, "little")
        chan = self.map.channels.get(ch_id) if self.map else None
        if chan is not None:
            frame["ch_name"] = chan.name
            if chan.width != len(value_bytes):
                # The map advertises a per-channel width the stub did not honor;
                # decode by the wire and surface the mismatch rather than failing.
                frame["width_note"] = f"map width {chan.width} != wire {len(value_bytes)}"

    # -- rendering ---------------------------------------------------------------

    def _render(self, frame: dict) -> str:
        name = frame.get("name")
        renderer = _RENDERERS.get(name)
        if renderer is not None:
            return renderer(frame, self.map)
        # Known type with no special renderer: show decoded fields generically.
        body = " ".join(f"{k}={_fmt(v)}" for k, v in _extra_fields(frame).items())
        return f"{name} seq={frame.get('seq')}" + (f" {body}" if body else "")


# -- module-level renderers (one per message; kept pure for testability) ---------


_HEADER_KEYS = ("name", "type", "seq", "args")


def _extra_fields(frame: dict) -> dict:
    """The decoded payload of a frame -- everything except the envelope/header keys
    (name/type/seq/args). Used for both Event.fields and the generic renderer so
    the two never disagree on what counts as a 'field'."""
    return {k: v for k, v in frame.items() if k not in _HEADER_KEYS}


def _stamp(text: str, tstamp: int | None) -> str:
    return f"[{tstamp:>10} us] {text}" if tstamp is not None else f"[{'':>10}   ] {text}"


def _fmt(value: object) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex(" ") or "(empty)"
    if isinstance(value, int):
        return f"0x{value:X}({value})"
    return str(value)


def _render_hello(frame: dict, mmap: MonitorMap | None) -> str:
    caps = frame.get("caps", 0)
    names = [n for n, bit in (
        ("mem_read", proto.CAP_MEM_READ), ("mem_write", proto.CAP_MEM_WRITE),
        ("sw_bp", proto.CAP_SW_BREAKPOINTS), ("tick", proto.CAP_TARGET_TICK),
        ("rx_cmd", proto.CAP_RX_COMMANDS)) if caps & bit]
    return (f"HELLO ver={frame.get('ver')} devid=0x{frame.get('devid', 0):04X} "
            f"tier={frame.get('tier')} channels={frame.get('ch_count')} "
            f"caps=0x{caps:02X}[{','.join(names)}]")


def _render_trace(frame: dict, mmap: MonitorMap | None) -> str:
    if "decode_error" in frame:
        return (f"TRACE ch={frame.get('ch')} (undecoded: {frame['decode_error']}) "
                f"args={bytes(frame.get('args', b'')).hex()}")
    label = frame.get("ch_name") or f"ch{frame.get('ch')}"
    value = frame.get("value", 0)
    tick = frame.get("tick")
    tickpart = f" tick={tick}" if tick is not None else ""
    return f"TRACE {label} = 0x{value:X} ({value}){tickpart}"


def _render_log(frame: dict, mmap: MonitorMap | None) -> str:
    text = bytes(frame.get("text", b"")).decode("ascii", "replace").rstrip("\n")
    return f"LOG: {text}"


def _render_mem_data(frame: dict, mmap: MonitorMap | None) -> str:
    addr = frame.get("addr", 0)
    data = bytes(frame.get("bytes", b""))
    sym = mmap.symbol_at(addr) if mmap else None
    label = f" ({sym})" if sym else ""
    return f"MEM_DATA addr=0x{addr:04X}{label} = {data.hex(' ') or '(empty)'}"


def _render_bp_hit(frame: dict, mmap: MonitorMap | None) -> str:
    pc = frame.get("pc")
    pcpart = f" pc=0x{pc:04X}" if pc is not None else ""
    nf = frame.get("nframe")
    nfpart = f" nframe={nf}" if nf is not None else ""
    return f"BP_HIT bp={frame.get('bp_id')}{pcpart}{nfpart} (target halted, awaiting CONTINUE)"


def _render_ack(frame: dict, mmap: MonitorMap | None) -> str:
    return f"ACK ref_seq={frame.get('ref_seq')}"


def _render_nak(frame: dict, mmap: MonitorMap | None) -> str:
    code = frame.get("code")
    cname = _NAK_NAMES.get(code) if code is not None else None
    codepart = f" code={code}({cname})" if code is not None else ""
    return f"NAK ref_seq={frame.get('ref_seq')}{codepart}"


_NAK_NAMES = {v: k for k, v in proto.NAK_CODES.items()}

_RENDERERS = {
    "HELLO": _render_hello,
    "TRACE": _render_trace,
    "LOG": _render_log,
    "MEM_DATA": _render_mem_data,
    "BP_HIT": _render_bp_hit,
    "ACK": _render_ack,
    "NAK": _render_nak,
}


def _augment_bp_hit(frame: dict) -> None:
    """BP_HIT carries bp_id plus, per build, an optional pc (2 B) then nframe (1 B)
    (01 SS 4). The map does not record which optionals a build emits, so presence is
    inferred from LEN: 1=bp_id, 3=+pc, 4=+pc+nframe. Any other length is flagged
    rather than silently truncated -- we cannot disambiguate (e.g. nframe-without-pc)
    without a per-build layout descriptor (05 SS 9 open question)."""
    args = bytes(frame.get("args", b""))
    if not args:
        frame["decode_error"] = "BP_HIT has no bp_id byte"
        return
    frame["bp_id"] = args[0]
    if len(args) == 1:
        return
    if len(args) in (3, 4):
        frame["pc"] = int.from_bytes(args[1:3], "little")
        if len(args) == 4:
            frame["nframe"] = args[3]
        return
    frame["decode_error"] = (f"BP_HIT len {len(args)} has no known optional layout "
                             f"(extra={args[1:].hex()})")


def _augment_nak(frame: dict) -> None:
    """NAK.code is optional (01 SS 4): ref_seq(1)[, code(1)]."""
    args = bytes(frame.get("args", b""))
    if not args:
        frame["decode_error"] = "NAK has no ref_seq byte"
        return
    frame["ref_seq"] = args[0]
    if len(args) >= 2:
        frame["code"] = args[1]


# -- command encoding (host -> target), for the interactive side of debug-monitor


def encode_command(name: str, seq: int, mmap: MonitorMap | None = None,
                   *, bind: dict | None = None, **fields: object) -> bytes:
    """Encode a host->target command frame (PING/READ_MEM/SET_BP/CLR_BP/CONTINUE/
    SET_TRACE/WRITE_MEM). A pure wrapper over cdl_codec.encode that adds symbol
    resolution: pass ``addr`` as a symbol *name* (str) and it is resolved through
    the map's symbol table (01 SS 6). Raises ValueError for a non-H2T message."""
    msg = proto.BY_NAME.get(name)
    if msg is None or msg.group is not proto.Group.H2T:
        raise ValueError(f"{name!r} is not a host->target command")
    if "addr" in fields and isinstance(fields["addr"], str):
        if mmap is None:
            raise ValueError(f"addr {fields['addr']!r} is a symbol but no map was given")
        fields = dict(fields)
        try:
            fields["addr"] = mmap.resolve_symbol(fields["addr"])
        except KeyError as exc:
            # Normalize to ValueError so every bad-input path of encode_command has
            # one exception type for a caller to catch.
            raise ValueError(f"unknown symbol {fields['addr']!r} (not in map)") from exc
    return cdl_codec.encode(name, seq, bind=bind, **fields)
