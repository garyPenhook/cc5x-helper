"""cdl_codec.py -- encode/decode CDL frames, driven by the cdl_proto spec.

Pure functions + one stateful deframer; no I/O (serial belongs to debug-monitor,
P4). This is the protocol's reference implementation (04 SS 3). All framing, CRC,
and stuffing constants come from cdl_proto, so there is one source of truth
(05 SS 4.1). The deframer is the proven SLIP state machine from the probe's
relay_decode.py, generalized to the full catalogue.

Two layers, kept separate on purpose:
  - The *frame envelope* (FLAG / stuffing / CRC8 / TYPE / SEQ / LEN) is fully
    generic and always decodable -- this is the hard, resync-safe part.
  - *Field interpretation* of the ARG bytes is generic for fully-determined
    messages (only fixed-width ints + an optional trailing BYTES). Messages with
    optional fields or a per-build variable-width value (TRACE, BP_HIT, NAK)
    need an explicit ``bind`` resolving which optionals are present and each
    VarVal width (05 SS 9) -- the deframer leaves those as raw ``args``.
"""
from __future__ import annotations

from collections.abc import Iterator

from . import cdl_proto as proto


def crc8(data: bytes) -> int:
    """CRC-8 over ``data`` using the cdl_proto parameters (poly 0x07, init 0x00)."""
    crc = proto.CDL_CRC.init
    poly = proto.CDL_CRC.poly
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def _stuff(out: bytearray, b: int) -> None:
    """Append ``b`` to ``out`` with SLIP byte-stuffing (01 SS 3)."""
    if b in (proto.CDL_FLAG, proto.CDL_ESC):
        out.append(proto.CDL_ESC)
        out.append(b ^ proto.CDL_ESC_XOR)
    else:
        out.append(b)


def is_fully_determined(msg: proto.Msg) -> bool:
    """True when ``msg``'s ARG layout is fixed without a per-build ``bind`` (no
    optional fields and no variable-width value)."""
    return all(not f.optional and not isinstance(f.kind, proto.VarVal) for f in msg.fields)


def _resolved_fields(msg: proto.Msg, bind: dict | None) -> list[tuple[proto.Field, int | None]]:
    """Resolve ``msg``'s fields against ``bind`` into (field, width) pairs in wire
    order; width is None for the trailing BYTES tail. Optional fields are included
    only when ``bind[name]`` is truthy; VarVal widths come from ``bind[name]``."""
    bind = bind or {}
    out: list[tuple[proto.Field, int | None]] = []
    for fld in msg.fields:
        kind = fld.kind
        if fld.optional and not bind.get(fld.name, False):
            continue
        if isinstance(kind, proto.IntLE):
            out.append((fld, kind.width))
        elif isinstance(kind, proto.VarVal):
            width = bind.get(fld.name)
            if not isinstance(width, int):
                raise ValueError(f"{msg.name}.{fld.name}: VarVal needs an int width in bind")
            if not kind.min_width <= width <= kind.max_width:
                raise ValueError(f"{msg.name}.{fld.name}: width {width} outside "
                                 f"{kind.min_width}..{kind.max_width}")
            out.append((fld, width))
        else:  # Bytes tail
            out.append((fld, None))
    return out


def encode_args(name: str, *, bind: dict | None = None, **values: object) -> bytes:
    """Serialize just the ARG payload for message ``name`` (no header/framing)."""
    msg = proto.BY_NAME[name]
    fields = _resolved_fields(msg, bind)
    expected = {fld.name for fld, _ in fields}
    given = set(values)
    if given != expected:
        missing = expected - given
        extra = given - expected
        raise ValueError(f"{name}: field mismatch (missing={sorted(missing)}, "
                         f"unexpected={sorted(extra)})")
    args = bytearray()
    for fld, width in fields:
        value = values[fld.name]
        if width is None:  # BYTES tail
            args += bytes(value)  # type: ignore[arg-type]
        elif isinstance(value, (bytes, bytearray)):
            if len(value) != width:
                raise ValueError(f"{name}.{fld.name}: expected {width} bytes, got {len(value)}")
            args += bytes(value)
        else:
            args += int(value).to_bytes(width, "little")  # type: ignore[call-overload]
    return bytes(args)


def encode(name: str, seq: int, *, bind: dict | None = None, **values: object) -> bytes:
    """Encode a full, FLAG-delimited, stuffed CDL frame for message ``name``."""
    msg = proto.BY_NAME[name]
    args = encode_args(name, bind=bind, **values)
    if len(args) > 255:
        raise ValueError(f"{name}: ARG payload {len(args)} > 255 (LEN is one byte)")
    body = bytes([msg.type, seq & 0xFF, len(args)]) + args
    out = bytearray([proto.CDL_FLAG])
    for b in body:
        _stuff(out, b)
    _stuff(out, crc8(body))
    out.append(proto.CDL_FLAG)
    return bytes(out)


def decode_args(name: str, args: bytes, *, bind: dict | None = None) -> dict:
    """Decode ARG bytes for message ``name`` into a {field: value} dict. Ints come
    back as ints; the BYTES tail as ``bytes``. Raises on truncation or trailing
    bytes (a wrong ``bind`` for a variable-layout message)."""
    fields = _resolved_fields(proto.BY_NAME[name], bind)
    out: dict[str, object] = {}
    off = 0
    for fld, width in fields:
        if width is None:  # BYTES tail consumes the remainder
            out[fld.name] = bytes(args[off:])
            off = len(args)
        else:
            if off + width > len(args):
                raise ValueError(f"{name}: truncated ARG at {fld.name}")
            out[fld.name] = int.from_bytes(args[off:off + width], "little")
            off += width
    if off != len(args):
        raise ValueError(f"{name}: {len(args) - off} trailing ARG byte(s) undecoded")
    return out


class Deframer:
    """Feed bytes; yields decoded frames as dicts. Resyncs on FLAG. Mirrors the
    probe's relay_decode.Deframer behaviour: malformed/CRC-failed/dangling-escape
    frames are surfaced as ``{"error": ...}`` and never desync the next frame.

    A good frame is ``{"name", "type", "seq", "args"}`` plus the decoded fields
    when the message is fully determined (RELAY/STATUS/HELLO/MEM_DATA/commands).
    An unknown TYPE yields ``name=None`` with raw ``args`` (01 SS 9: ignore/NAK is
    the caller's policy)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in = False
        self._esc = False
        self._bad = False    # invalid escape seen in the current frame

    def feed(self, chunk: bytes) -> Iterator[dict]:
        for b in chunk:
            if b == proto.CDL_FLAG:
                if self._in and self._buf:
                    if self._bad or self._esc:   # dangling/invalid escape -> truncated
                        yield {"error": "escape", "raw": bytes(self._buf).hex()}
                    else:
                        frame = self._decode(bytes(self._buf))
                        if frame is not None:
                            yield frame
                self._buf.clear()
                self._in = True
                self._esc = False
                self._bad = False
            elif not self._in:
                continue
            elif b == proto.CDL_ESC:
                self._esc = True
            elif self._esc:
                orig = b ^ proto.CDL_ESC_XOR
                if orig in (proto.CDL_FLAG, proto.CDL_ESC):
                    self._buf.append(orig)
                else:
                    self._bad = True             # malformed escape -> drop frame
                self._esc = False
            else:
                self._buf.append(b)

    @staticmethod
    def _decode(inner: bytes) -> dict:
        if len(inner) < 4:                       # TYPE+SEQ+LEN+CRC minimum
            return {"error": "malformed", "raw": inner.hex()}
        body, crc = inner[:-1], inner[-1]
        if crc8(body) != crc:
            return {"error": "crc", "raw": inner.hex()}
        type_, seq, length = body[0], body[1], body[2]
        args = body[3:]
        if length != len(args):
            return {"error": "len", "raw": inner.hex()}
        msg = proto.BY_TYPE.get(type_)
        frame: dict = {"name": msg.name if msg else None, "type": type_,
                       "seq": seq, "args": bytes(args)}
        if msg is not None and is_fully_determined(msg):
            try:
                frame.update(decode_args(msg.name, bytes(args)))
            except ValueError as exc:
                return {"error": "decode", "raw": inner.hex(), "detail": str(exc)}
        return frame
