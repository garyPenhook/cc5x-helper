"""cdl_proto.py -- single source of truth for the CC5X Debug Link (CDL) wire protocol.

Everything that defines *bytes on the wire* -- framing, CRC, message TYPE codes,
and per-message ARG field layouts -- lives here and nowhere else. The Python codec
(cdl_codec.py, P1 step 2), the generated C header (cdl_proto.h), and debuggen's
emitted target stub all derive from this module, so a protocol change is a
one-file edit.

References:
  - wire spec:            cc5x-debug-probe/01-debug-link-protocol.md (esp. SS 3, 4, 9)
  - single-source design: cc5x-debug-probe/04-software-stack.md SS 4
  - P1 design:            cc5x-debug-probe/05-cdl-proto-codec.md

This module is pure data + structural validation: no I/O and no encode/decode
(that is cdl_codec.py). Bank/address arithmetic and tier/footprint decisions are
NOT here -- they stay in debuggen.py (05 SS 3.3).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# --- Protocol version. Carried only in HELLO.ver, never in the frame header
#     (01 SS 3/SS 9), so every other frame spends one fewer byte. -----------------
CDL_PROTOCOL_VERSION = (0, 1)

# --- Framing (01 SS 3). Byte stuffing is defined once as XOR-0x20: 0x7E^0x20=0x5E
#     and 0x7D^0x20=0x5D, identical to the literal pairs the probe firmware emits
#     (firmware/probe-f042/src/relay.c). Each generator renders its own idiom. ---
CDL_FLAG = 0x7E
CDL_ESC = 0x7D
CDL_ESC_XOR = 0x20


@dataclass(frozen=True)
class CrcSpec:
    """CRC parameters. CDL uses CRC-8/SMBUS-style over TYPE..last ARG (01 SS 3)."""

    poly: int
    init: int
    width: int


CDL_CRC = CrcSpec(poly=0x07, init=0x00, width=8)
# Back-compat aliases for debuggen's current constant names (eases the 05 SS 5 swap).
CDL_CRC_POLY = CDL_CRC.poly
CDL_CRC_INIT = CDL_CRC.init

# --- HELLO.caps bitfield (01 SS 4 + the A-full/A-min capability split) ----------
CAP_MEM_READ = 0x01
CAP_MEM_WRITE = 0x02
CAP_SW_BREAKPOINTS = 0x04
CAP_TARGET_TICK = 0x08
CAP_RX_COMMANDS = 0x10

# --- NAK codes (01 SS 4) --------------------------------------------------------
NAK_CODES = {
    "BAD_ADDR": 1,
    "SIDE_EFFECT": 2,
    "WRITE_DENIED": 3,
    "BAD_LEN": 4,
    "UNKNOWN_TYPE": 5,
}

# --- Wire tier byte for HELLO.tier. The A-full vs A-min distinction lives in caps
#     + the map, not on the wire, so "full" and "min" share tier byte 1 (01 SS 4). -
TIER_WIRE = {"full": 1, "min": 1, "trace": 2, "toggle": 3}

# --- Protocol limits visible on the wire (01 SS 4) ------------------------------
MAX_BP = 8              # software breakpoint ids 0..MAX_BP-1; one uns8 bitmask
MAX_CHANNELS_TRACE = 4  # trace channels per Tier-A/B build


class Group(Enum):
    """Message direction/origin. Each group owns a TYPE-code range (01 SS 4/SS 9)."""

    T2H = "T2H"      # target -> host:                    0x01..0x0F
    H2T = "H2T"      # host -> target (Tier A only):      0x81..0x8F
    PROBE = "PROBE"  # probe/companion -> host envelopes: 0xF0..0xFF


_GROUP_RANGE = {
    Group.T2H: (0x01, 0x0F),
    Group.H2T: (0x81, 0x8F),
    Group.PROBE: (0xF0, 0xFF),
}


# --- Field model. A message's ARG payload is an ordered tuple of fields. --------
@dataclass(frozen=True)
class IntLE:
    """Fixed-width little-endian unsigned integer of `width` bytes."""

    width: int


@dataclass(frozen=True)
class Bytes:
    """Variable-length tail; its length is LEN minus the fixed prefix. Must be the
    last field of a message."""


@dataclass(frozen=True)
class VarVal:
    """A value whose width (min_width..max_width bytes) is fixed per build at
    codegen time (e.g. TRACE.value, 1..4 bytes per channel). Little-endian."""

    min_width: int
    max_width: int


U8 = IntLE(1)
U16LE = IntLE(2)
U32LE = IntLE(4)
BYTES = Bytes()

FieldKind = IntLE | Bytes | VarVal


@dataclass(frozen=True)
class Field:
    name: str
    kind: FieldKind
    optional: bool = False   # gated per build/caps (e.g. TRACE.tick, BP_HIT.pc)


@dataclass(frozen=True)
class Msg:
    type: int
    name: str
    group: Group
    fields: tuple[Field, ...] = ()


def _f(name: str, kind: FieldKind, optional: bool = False) -> Field:
    return Field(name, kind, optional)


# --- The catalogue: the union of 01 SS 4's three tables. Order within each group
#     preserves debuggen's current emit order so the 05 SS 5 refactor is a no-op on
#     the generated header (the regression bar is an empty golden diff). ---------
MESSAGES: tuple[Msg, ...] = (
    # Target -> Host
    Msg(0x01, "HELLO", Group.T2H, (
        _f("ver", U8), _f("devid", U16LE), _f("caps", U8),
        _f("tier", U8), _f("ch_count", U8))),
    Msg(0x02, "TRACE", Group.T2H, (
        _f("ch", U8), _f("tick", U16LE, optional=True),
        _f("value", VarVal(1, 4)))),
    Msg(0x03, "LOG", Group.T2H, (_f("text", BYTES),)),
    Msg(0x04, "MEM_DATA", Group.T2H, (_f("addr", U16LE), _f("bytes", BYTES))),
    Msg(0x05, "BP_HIT", Group.T2H, (
        _f("bp_id", U8), _f("pc", U16LE, optional=True),
        _f("nframe", U8, optional=True))),
    Msg(0x06, "ACK", Group.T2H, (_f("ref_seq", U8),)),
    Msg(0x07, "NAK", Group.T2H, (_f("ref_seq", U8), _f("code", U8, optional=True))),
    # Host -> Target (Tier A only)
    Msg(0x81, "PING", Group.H2T),
    Msg(0x82, "READ_MEM", Group.H2T, (_f("addr", U16LE), _f("len", U8))),
    Msg(0x83, "WRITE_MEM", Group.H2T, (_f("addr", U16LE), _f("bytes", BYTES))),
    Msg(0x84, "SET_BP", Group.H2T, (_f("bp_id", U8),)),
    Msg(0x85, "CLR_BP", Group.H2T, (_f("bp_id", U8),)),
    Msg(0x86, "CONTINUE", Group.H2T, (_f("bp_id", U8),)),
    Msg(0x87, "SET_TRACE", Group.H2T, (_f("ch", U8), _f("on", U8))),
    # Probe/companion -> Host envelopes (01 SS 4; implemented in relay.c)
    Msg(0xF0, "RELAY", Group.PROBE, (_f("tstamp", U32LE), _f("data", BYTES))),
    Msg(0xF1, "STATUS", Group.PROBE, (_f("dropped", U32LE),)),
)

BY_NAME: dict[str, Msg] = {m.name: m for m in MESSAGES}
BY_TYPE: dict[int, Msg] = {m.type: m for m in MESSAGES}

# Back-compat name->code dicts for debuggen (05 SS 5), in catalogue (= emit) order.
MSG_TYPES_T2H = {m.name: m.type for m in MESSAGES if m.group is Group.T2H}
MSG_TYPES_H2T = {m.name: m.type for m in MESSAGES if m.group is Group.H2T}
MSG_TYPES_PROBE = {m.name: m.type for m in MESSAGES if m.group is Group.PROBE}


def validate_catalogue() -> list[str]:
    """Return a list of structural problems with MESSAGES (empty == valid).

    Checks the invariants the wire format depends on: unique TYPE codes, each
    TYPE inside its group's reserved range (01 SS 4/SS 9), and a variable-length
    BYTES tail only ever as the final field of a message.
    """
    errs: list[str] = []
    seen: dict[int, str] = {}
    for m in MESSAGES:
        if m.type in seen:
            errs.append(f"duplicate TYPE 0x{m.type:02X}: {seen[m.type]} and {m.name}")
        seen[m.type] = m.name
        lo, hi = _GROUP_RANGE[m.group]
        if not lo <= m.type <= hi:
            errs.append(f"{m.name} TYPE 0x{m.type:02X} outside {m.group.value} "
                        f"range 0x{lo:02X}..0x{hi:02X}")
        for i, fld in enumerate(m.fields):
            if isinstance(fld.kind, Bytes) and i != len(m.fields) - 1:
                errs.append(f"{m.name}.{fld.name}: BYTES tail must be the last field")
    return errs


_errs = validate_catalogue()
if _errs:   # fail fast at import -- an authoring error must never reach codegen
    raise ValueError("cdl_proto catalogue invalid:\n  " + "\n  ".join(_errs))
