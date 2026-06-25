"""CC5X Debug Link (CDL) target-stub generator.

Generates a per-device cooperative debug/trace stub (``cdl_monitor_<dev>.c/.h``) and a
PC-side decode map (``cdl_map_<dev>.json``) from the normalized device model
(:mod:`picmeta`) plus a ``debug`` manifest section. Pairs with the design docs in the
``cc5x-debug-probe`` repo (``01-debug-link-protocol.md`` .. ``04-software-stack.md``).

Design rules honored here:

* **No hardcoded register addresses or bit positions.** Every EUSART register name, the
  register a control/status bit lives in, and its bit position are read from device
  metadata; if a register or bit the requested tier needs is absent, generation fails
  with a clear error rather than emitting something the device does not have.
* **No invented datasheet facts.** The EUSART baud divisor (SPBRG) depends on the runtime
  ``Fosc``, which is *not* in pack metadata. The stub requires the value to be supplied
  (manifest ``transport.brg``) or emits ``#error`` forcing the user to define
  ``CDL_SPBRG_VALUE`` from their datasheet -- it never guesses one.
* **CC5X-safe emission.** Pack-derived tokens pass through the same ``_safe_identifier`` /
  ``_safe_comment`` discipline as :mod:`headergen`.
* **Tier capability floors are design rules, not measured facts.** The final tier must be
  confirmed against a measured CC5X budget (``02-target-footprint.md`` §6); this module
  picks a *provisional* tier and says so.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .cdl_proto import (
    CAP_MEM_READ,
    CAP_MEM_WRITE,
    CAP_RX_COMMANDS,
    CAP_SW_BREAKPOINTS,
    CAP_TARGET_TICK,
    CDL_CRC_POLY,
    CDL_ESC,
    CDL_ESC_XOR,
    CDL_FLAG,
    CDL_PROTOCOL_VERSION,
    MAX_BP,
    MAX_CHANNELS_TRACE,
    MSG_TYPES_H2T,
    MSG_TYPES_T2H,
    NAK_CODES,
    TIER_WIRE,
)
from .cdl_protogen import render_proto_defines
from .headergen import _safe_comment, _safe_identifier
from .picmeta import DeviceMetadata


# --------------------------------------------------------------------------------------
# CDL protocol constants now live in the single-source spec (:mod:`cdl_proto`, P1) and are
# imported above: framing (CDL_FLAG/ESC/ESC_XOR/CRC_POLY), the T2H/H2T message-type tables,
# HELLO.caps bits, NAK codes, the wire tier byte, and MAX_BP / MAX_CHANNELS_TRACE. The same
# spec drives the Python codec (cdl_codec) and the C header (cc5x-debug-probe 04 §4, 05).
# Footprint/tier-decision constants below are NOT wire facts and stay local (05 §3.3).
# --------------------------------------------------------------------------------------


# Tier capability floors -- DESIGN RULES from 02-target-footprint.md §2, NOT datasheet
# facts. Conservative starting estimates; the measured-budget gate (§6) can demote a part.
RAM_FULL_FLOOR = 512
FLASH_FULL_FLOOR = 4096
RAM_MIN_FLOOR = 256
FLASH_MIN_FLOOR = 2048
RAM_TRACE_FLOOR = 48
FLASH_TRACE_FLOOR = 1024

# Enhanced-midrange cores that predefine the FSR0/INDF0 linear-addressing registers the
# READ_MEM path relies on (CC5X declares these for these arch classes, not in pack SFRs).
ENHANCED_ARCHS = {"PIC14E", "PIC14EX"}

TIER_ORDER = ["toggle", "trace", "min", "full"]  # ascending capability
VALID_TIERS = {"auto", "full", "min", "trace", "toggle"}
VALID_TOGGLE_ENCODINGS = {"fixed-site", "pulse-width", "pulse-train"}
VALID_BREAKPOINTS = {"software", "none"}


# EUSART register/bit name candidates. The authoritative source is the device's own SFR /
# bit-field list; these are only the naming variants to look it up under (TXREG vs TX1REG
# vs TXREG1, etc.). Detection confirms presence in metadata before anything is emitted.
_EUSART_TX_DATA = ("TXREG", "TX1REG", "TXREG1")
_EUSART_RX_DATA = ("RCREG", "RC1REG", "RCREG1")
_EUSART_TXSTA = ("TXSTA", "TX1STA", "TXSTA1")
_EUSART_RCSTA = ("RCSTA", "RC1STA", "RCSTA1")
_EUSART_SPBRG = ("SPBRGL", "SPBRG", "SP1BRGL", "SPBRG1", "SPBRGL1")

_BIT_SPEN = ("SPEN",)
_BIT_TXEN = ("TXEN", "TXEN1")
_BIT_SYNC = ("SYNC", "SYNC1")
_BIT_BRGH = ("BRGH", "BRGH1")
_BIT_CREN = ("CREN", "CREN1")
_BIT_TRMT = ("TRMT", "TRMT1")
_BIT_RCIF = ("RCIF", "RC1IF")

_PIN_RE = re.compile(r"^R([A-K])([0-7])$")


# --------------------------------------------------------------------------------------
# Configuration model
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DebugChannel:
    name: str
    width: int
    id: int


@dataclass(frozen=True)
class DebugConfig:
    enabled: bool
    tier: str  # auto | full | min | trace | toggle
    tx_pin: str | None
    rx_pin: str | None
    baud: int | None
    brg: int | None  # explicit SPBRG value (never derived from Fosc)
    brgh: bool
    target_timestamp: bool
    breakpoints: str  # software | none
    write_mem: bool  # off by default (01 §6)
    channels: tuple[DebugChannel, ...]
    toggle_encoding: str
    toggle_pins: tuple[str, ...]


def _as_bool(value: object, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"debug.{label} must be a boolean, got {type(value).__name__}")
    return value


def _as_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"debug.{label} must be an integer, got {type(value).__name__}")
    return value


def _as_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"debug.{label} must be a string, got {type(value).__name__}")
    return value


def parse_debug_config(payload: object) -> DebugConfig:
    """Parse and shape-check the manifest ``debug`` section (a plain dict).

    Mirrors :mod:`project`'s strict-typing discipline: wrong JSON types are rejected at
    parse time. Semantic validation happens in :func:`validate_debug` once the device is
    known.
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("'debug' must be an object")

    transport = payload.get("transport") or {}
    if not isinstance(transport, dict):
        raise ValueError("debug.transport must be an object")
    toggle = payload.get("toggle") or {}
    if not isinstance(toggle, dict):
        raise ValueError("debug.toggle must be an object")

    tier = (_as_str(payload.get("tier"), "tier") or "auto").lower()

    channels: list[DebugChannel] = []
    channels_raw = payload.get("channels")
    if channels_raw is not None:
        if not isinstance(channels_raw, list):
            raise ValueError("debug.channels must be an array")
        seen: set[str] = set()
        for index, item in enumerate(channels_raw):
            if not isinstance(item, dict):
                raise ValueError(f"debug.channels[{index}] must be an object")
            name = _as_str(item.get("name"), f"channels[{index}].name")
            if not name:
                raise ValueError(f"debug.channels[{index}].name is required")
            if name in seen:
                raise ValueError(f"debug.channels[{index}]: duplicate channel name {name!r}")
            seen.add(name)
            width = _as_int(item.get("width"), f"channels[{index}].width")
            channels.append(
                DebugChannel(name=name, width=width if width is not None else 1, id=index)
            )

    toggle_pins_raw = toggle.get("pins")
    toggle_pins: list[str] = []
    if toggle_pins_raw is not None:
        if not isinstance(toggle_pins_raw, list) or any(
            not isinstance(p, str) for p in toggle_pins_raw
        ):
            raise ValueError("debug.toggle.pins must be an array of strings")
        toggle_pins = list(toggle_pins_raw)

    return DebugConfig(
        enabled=_as_bool(payload.get("enabled"), "enabled", default=True),
        tier=tier,
        tx_pin=_as_str(transport.get("tx_pin"), "transport.tx_pin"),
        rx_pin=_as_str(transport.get("rx_pin"), "transport.rx_pin"),
        baud=_as_int(transport.get("baud"), "transport.baud"),
        brg=_as_int(transport.get("brg"), "transport.brg"),
        brgh=_as_bool(transport.get("brgh"), "transport.brgh", default=True),
        target_timestamp=_as_bool(
            payload.get("target_timestamp"), "target_timestamp", default=False
        ),
        breakpoints=(_as_str(payload.get("breakpoints"), "breakpoints") or "software").lower(),
        write_mem=_as_bool(payload.get("write_mem"), "write_mem", default=False),
        channels=tuple(channels),
        toggle_encoding=(_as_str(toggle.get("encoding"), "toggle.encoding") or "fixed-site").lower(),
        toggle_pins=tuple(toggle_pins),
    )


# --------------------------------------------------------------------------------------
# Capability detection from device metadata
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EusartRegs:
    tx_data: str
    rx_data: str | None
    txsta: str
    rcsta: str
    spbrg: str
    spen_bit: int
    txen_bit: int
    sync_bit: int
    brgh_bit: int | None
    cren_bit: int | None
    trmt_reg: str
    trmt_bit: int
    rcif_reg: str | None
    rcif_bit: int | None

    @property
    def has_rx(self) -> bool:
        """True when the metadata exposes a usable receive path (data reg + RCIF
        flag). Gates both the CREN receiver-enable in cdl_init and the cdl_poll/
        dispatch emission, so the two never disagree."""
        return (
            self.rx_data is not None
            and self.rcif_reg is not None
            and self.rcif_bit is not None
        )


@dataclass(frozen=True)
class DeviceCaps:
    ram_bytes: int
    flash_words: int
    has_eusart: bool
    has_free_timer: bool
    eusart: EusartRegs | None
    arch: str | None


def _sum_ram_bytes(metadata: DeviceMetadata) -> int:
    """Count usable data RAM, de-duplicating mirrored regions.

    The pack `.ini` lists the access/common block once per bank (0x70-0x7F mirrored as
    0xF0-0xFF, 0x170-0x17F, ... across every bank), so naively summing COMMON inflates RAM
    by (banks-1) x block_size. We union concrete addresses from RAMBANK and count only a
    *single* instance of the common block -- which usually already overlaps bank 0's GPR
    range, so it contributes nothing extra. This matches the device's true GPR size.
    """
    covered: set[int] = set()
    for r in metadata.ram_ranges:
        if r.end >= r.start:
            covered.update(range(r.start, r.end + 1))
    if metadata.common_ranges:
        block = min(metadata.common_ranges, key=lambda r: r.start)
        if block.end >= block.start:
            covered.update(range(block.start, block.end + 1))
    return len(covered)


def _first_present(names: tuple[str, ...], present: set[str]) -> str | None:
    for name in names:
        if name in present:
            return name
    return None


def _field(
    names: tuple[str, ...], field_info: dict[str, tuple[str | None, int]]
) -> tuple[str | None, int | None]:
    """Return (containing_register, bit_position) for the first present field name."""
    for name in names:
        if name in field_info:
            return field_info[name]
    return (None, None)


def detect_caps(metadata: DeviceMetadata) -> DeviceCaps:
    sfr_names = {s.name for s in metadata.sfrs}
    # address -> canonical 8-bit register name (first wins, mirroring headergen).
    addr_to_reg: dict[int, str] = {}
    for s in metadata.sfrs:
        if s.width == 8 and s.address not in addr_to_reg:
            addr_to_reg[s.address] = s.name
    # 1-bit field name -> (containing register, bit position). First occurrence wins
    # (bank mirrors repeat the name).
    field_info: dict[str, tuple[str | None, int]] = {}
    for f in metadata.sfr_fields:
        if f.width == 1 and f.name not in field_info:
            field_info[f.name] = (addr_to_reg.get(f.address), f.bit_position)

    tx_data = _first_present(_EUSART_TX_DATA, sfr_names)
    txsta = _first_present(_EUSART_TXSTA, sfr_names)
    rcsta = _first_present(_EUSART_RCSTA, sfr_names)
    spbrg = _first_present(_EUSART_SPBRG, sfr_names)
    rx_data = _first_present(_EUSART_RX_DATA, sfr_names)

    _, spen = _field(_BIT_SPEN, field_info)
    _, txen = _field(_BIT_TXEN, field_info)
    _, sync = _field(_BIT_SYNC, field_info)
    _, brgh = _field(_BIT_BRGH, field_info)
    _, cren = _field(_BIT_CREN, field_info)
    trmt_reg, trmt = _field(_BIT_TRMT, field_info)
    rcif_reg, rcif = _field(_BIT_RCIF, field_info)

    # A usable EUSART TX path needs the data/status/baud registers plus the SPEN/TXEN/SYNC
    # control bits and a TRMT status bit (with a resolvable containing register). Anything
    # missing => "no usable EUSART", so tier selection falls back rather than half-wiring.
    eusart: EusartRegs | None = None
    has_eusart = (
        None not in (tx_data, txsta, rcsta, spbrg, spen, txen, sync, trmt)
        and trmt_reg is not None
    )
    if has_eusart:
        eusart = EusartRegs(
            tx_data=tx_data,
            rx_data=rx_data,
            txsta=txsta,
            rcsta=rcsta,
            spbrg=spbrg,
            spen_bit=spen,
            txen_bit=txen,
            sync_bit=sync,
            brgh_bit=brgh,
            cren_bit=cren,
            trmt_reg=trmt_reg,
            trmt_bit=trmt,
            rcif_reg=rcif_reg,
            rcif_bit=rcif,
        )

    has_free_timer = "TMR1L" in sfr_names and "TMR1H" in sfr_names

    return DeviceCaps(
        ram_bytes=_sum_ram_bytes(metadata),
        flash_words=metadata.rom_size_words or 0,
        has_eusart=has_eusart,
        has_free_timer=has_free_timer,
        eusart=eusart,
        arch=metadata.ini_arch,
    )


# --------------------------------------------------------------------------------------
# Tier selection
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TierDecision:
    tier: str  # full | min | trace | toggle
    provisional: bool
    forced: bool
    reason: str


def _auto_tier(caps: DeviceCaps) -> tuple[str, str]:
    if caps.has_eusart and caps.ram_bytes >= RAM_FULL_FLOOR and caps.flash_words >= FLASH_FULL_FLOOR:
        return "full", (
            f"EUSART + RAM {caps.ram_bytes}B>={RAM_FULL_FLOOR} + "
            f"flash {caps.flash_words}w>={FLASH_FULL_FLOOR}"
        )
    if caps.has_eusart and caps.ram_bytes >= RAM_MIN_FLOOR and caps.flash_words >= FLASH_MIN_FLOOR:
        return "min", (
            f"EUSART + RAM {caps.ram_bytes}B>={RAM_MIN_FLOOR} + "
            f"flash {caps.flash_words}w>={FLASH_MIN_FLOOR} (below A-full floor)"
        )
    if caps.flash_words >= FLASH_TRACE_FLOOR and caps.ram_bytes >= RAM_TRACE_FLOOR:
        return "trace", (
            f"flash {caps.flash_words}w>={FLASH_TRACE_FLOOR} + RAM {caps.ram_bytes}B>="
            f"{RAM_TRACE_FLOOR} (no usable EUSART or below A-min floor)"
        )
    return "toggle", (
        f"RAM {caps.ram_bytes}B / flash {caps.flash_words}w below trace floor, or baseline core"
    )


def select_tier(config: DebugConfig, caps: DeviceCaps) -> TierDecision:
    """Pick the provisional tier; honor a manifest force-*down* but never force *up*."""
    auto_tier, auto_reason = _auto_tier(caps)
    if config.tier == "auto":
        return TierDecision(tier=auto_tier, provisional=True, forced=False, reason=f"auto: {auto_reason}")
    requested = config.tier
    if requested not in TIER_ORDER:
        raise ValueError(f"unknown tier {requested!r}")
    if TIER_ORDER.index(requested) > TIER_ORDER.index(auto_tier):
        raise ValueError(
            f"manifest forces tier {requested!r} but the device only supports {auto_tier!r} "
            f"({auto_reason}); a tier cannot be forced higher than the resources allow"
        )
    forced = requested != auto_tier
    reason = (
        f"forced down from {auto_tier!r} ({auto_reason})" if forced else f"matches auto ({auto_reason})"
    )
    return TierDecision(tier=requested, provisional=True, forced=forced, reason=reason)


def validate_debug(config: DebugConfig, caps: DeviceCaps, decision: TierDecision) -> list[str]:
    errors: list[str] = []
    if config.tier not in VALID_TIERS:
        errors.append(f"debug.tier must be one of {sorted(VALID_TIERS)}, got {config.tier!r}")
    if config.breakpoints not in VALID_BREAKPOINTS:
        errors.append(f"debug.breakpoints must be one of {sorted(VALID_BREAKPOINTS)}")
    if config.toggle_encoding not in VALID_TOGGLE_ENCODINGS:
        errors.append(f"debug.toggle.encoding must be one of {sorted(VALID_TOGGLE_ENCODINGS)}")
    for ch in config.channels:
        if ch.width not in (1, 2):
            errors.append(f"channel {ch.name!r}: width must be 1 or 2 (got {ch.width})")
    if config.tx_pin and not _PIN_RE.match(config.tx_pin.upper()):
        errors.append(f"debug.transport.tx_pin {config.tx_pin!r} is not a Rxn pin name")
    if config.rx_pin and not _PIN_RE.match(config.rx_pin.upper()):
        errors.append(f"debug.transport.rx_pin {config.rx_pin!r} is not a Rxn pin name")

    tier = decision.tier
    if tier in ("min", "trace") and len(config.channels) > MAX_CHANNELS_TRACE:
        errors.append(
            f"tier {tier!r} allows at most {MAX_CHANNELS_TRACE} trace channels "
            f"(got {len(config.channels)})"
        )
    if tier in ("full", "min"):
        if caps.eusart is None:
            errors.append(f"tier {tier!r} needs a hardware EUSART but none was detected in metadata")
        elif config.rx_pin and caps.eusart.rx_data is None:
            errors.append("rx_pin requested but no EUSART receive register (RCREG) found in metadata")
        if config.brg is None:
            errors.append(
                "tier needs an EUSART baud divisor: set debug.transport.brg (the SPBRG value for "
                "your Fosc/baud from the datasheet) -- it is not derivable from pack metadata"
            )
        # READ_MEM uses FSR0/INDF0 linear addressing, predefined only on enhanced cores.
        if (caps.arch or "").upper() not in ENHANCED_ARCHS:
            errors.append(
                f"tier {tier!r} (monitor) needs an enhanced-midrange core (one of "
                f"{sorted(ENHANCED_ARCHS)}) for FSR0/INDF0 memory access; device arch is "
                f"{caps.arch!r}"
            )
    if tier == "trace" and caps.eusart is None:
        # The trace tier currently emits an EUSART TX-only path. Bit-bang TX (Tier B for
        # parts with no EUSART) is not implemented yet -- fail clearly here instead of
        # crashing in code emission. Use 'toggle', or pick an EUSART-capable part.
        errors.append(
            "tier 'trace' on a device with no hardware EUSART would need bit-bang TX, which "
            "is not implemented yet; use tier 'toggle' or select an EUSART-capable part"
        )
    if tier == "toggle":
        if config.toggle_encoding == "fixed-site" and len(config.toggle_pins) < max(
            1, len(config.channels)
        ):
            errors.append("toggle fixed-site encoding needs one pin per marked site in debug.toggle.pins")
    if config.write_mem and tier not in ("full", "min"):
        errors.append("write_mem requires a monitor tier (full/min)")
    return errors


# --------------------------------------------------------------------------------------
# Map generation
# --------------------------------------------------------------------------------------


def _device_id(metadata: DeviceMetadata) -> int:
    # Pack `.ini` PROCID is BARE hex (e.g. 'A305', '1509') -- the same convention as
    # ROMSIZE, which picmeta parses base-16. Using base 0 would reject 'A305' (-> 0) and
    # silently misparse '1509' as decimal. Parse base 16; int() still accepts a 0x prefix.
    procid = metadata.ini_procid
    if procid:
        try:
            return int(procid, 16) & 0xFFFF
        except ValueError:
            pass
    return 0


def _capabilities(config: DebugConfig, caps: DeviceCaps, decision: TierDecision) -> int:
    bits = 0
    if decision.tier in ("full", "min"):
        bits |= CAP_MEM_READ | CAP_RX_COMMANDS
        if config.breakpoints == "software":
            bits |= CAP_SW_BREAKPOINTS
        if config.write_mem:
            bits |= CAP_MEM_WRITE
        # NOTE: CAP_TARGET_TICK is intentionally NOT advertised: the v1 stub does not yet
        # read a timer or append a target timestamp to TRACE frames. Advertising it would
        # make the PC decoder expect timestamp bytes the target never sends. The config
        # field (target_timestamp) and the cap bit are reserved for when it is implemented.
    return bits


def build_map(
    metadata: DeviceMetadata, config: DebugConfig, caps: DeviceCaps, decision: TierDecision
) -> dict:
    major, minor = CDL_PROTOCOL_VERSION
    caps_bits = _capabilities(config, caps, decision)
    payload = {
        "schema": "cdl-map/0.1",
        "protocol": {
            "version": f"{major}.{minor}",
            "flag": CDL_FLAG,
            "esc": CDL_ESC,
            "esc_xor": CDL_ESC_XOR,
            "crc_poly": CDL_CRC_POLY,
            "message_types": {**MSG_TYPES_T2H, **MSG_TYPES_H2T},
            "nak_codes": dict(NAK_CODES),
        },
        "device": metadata.device,
        "device_id": _device_id(metadata),
        "arch": metadata.ini_arch,
        "tier": decision.tier,
        "tier_wire": TIER_WIRE.get(decision.tier, 0),
        "tier_provisional": decision.provisional,
        "tier_forced": decision.forced,
        "tier_reason": decision.reason,
        "capabilities": {
            "bits": caps_bits,
            "mem_read": bool(caps_bits & CAP_MEM_READ),
            "mem_write": bool(caps_bits & CAP_MEM_WRITE),
            "sw_breakpoints": bool(caps_bits & CAP_SW_BREAKPOINTS),
            "target_tick": bool(caps_bits & CAP_TARGET_TICK),
            "rx_commands": bool(caps_bits & CAP_RX_COMMANDS),
        },
        "channels": [{"name": ch.name, "id": ch.id, "width": ch.width} for ch in config.channels],
        # Populated from the CC5X .var file after a build (01 §6 / 02 §6). Empty until then.
        "symbols": {},
        "budget": {
            "measured": False,
            "note": "RAM/code occupation is unknown until the stub is compiled; parse .occ "
            "and fill this in (02 §6 acceptance gate).",
        },
    }
    if decision.tier == "toggle":
        payload["toggle"] = {
            "encoding": config.toggle_encoding,
            "pins": list(config.toggle_pins),
            "sites": [{"id": ch.id, "name": ch.name} for ch in config.channels],
        }
    return payload


# --------------------------------------------------------------------------------------
# CC5X stub emission
# --------------------------------------------------------------------------------------


def _short(device: str) -> str:
    return device[3:] if device.upper().startswith("PIC") else device


def _guard(device: str) -> str:
    return f"CDL_MONITOR_{_safe_identifier(device).upper()}_H"


def _pin_tris(pin: str, metadata: DeviceMetadata) -> tuple[str, int]:
    m = _PIN_RE.match(pin.upper())
    if not m:
        raise ValueError(f"pin {pin!r} is not a valid Rxn name")
    port_letter, bit = m.group(1), int(m.group(2))
    tris = f"TRIS{port_letter}"
    if tris not in {s.name for s in metadata.sfrs}:
        raise ValueError(f"pin {pin!r} maps to {tris}, which is not present in device metadata")
    return tris, bit


def _render_header(
    metadata: DeviceMetadata, config: DebugConfig, caps: DeviceCaps, decision: TierDecision
) -> str:
    guard = _guard(metadata.device)
    caps_bits = _capabilities(config, caps, decision)
    lines: list[str] = [
        "// Generated by cc5x-helper debug-stub -- DO NOT EDIT.",
        f"// CDL target stub for {_safe_comment(metadata.device)}  "
        f"(tier {decision.tier}, provisional={str(decision.provisional).lower()})",
        f"// {_safe_comment(decision.reason)}",
        f"#ifndef {guard}",
        f"#define {guard}",
        "",
        # Device-independent constants from the single-source spec (P1); the same
        # renderer backs the standalone cdl_proto.h so the two cannot drift.
        *render_proto_defines(),
    ]
    lines.append(f"#define CDL_DEVID_HI      0x{(_device_id(metadata) >> 8) & 0xFF:02X}")
    lines.append(f"#define CDL_DEVID_LO      0x{_device_id(metadata) & 0xFF:02X}")
    lines.append(f"#define CDL_TIER          0x{TIER_WIRE.get(decision.tier, 0):02X}")
    lines.append(f"#define CDL_CAPS          0x{caps_bits:02X}")
    lines.append(f"#define CDL_CH_COUNT      {len(config.channels)}")
    lines.append("")
    if config.channels:
        lines.append("// --- trace channels ---")
        for ch in config.channels:
            lines.append(f"#define CDL_CH_{_safe_identifier(ch.name):<12} {ch.id}")
        lines.append("")

    lines.append("// --- public API ---")
    lines.append("void cdl_init(void);")
    if decision.tier in ("full", "min", "trace"):
        lines.append("void cdl_trace(uns8 ch, uns16 value);")
        lines.append("#define CDL_TRACE(name, value) cdl_trace(CDL_CH_##name, (uns16)(value))")
    if decision.tier in ("full", "min"):
        lines.append("void cdl_poll(void);")
        lines.append("#define CDL_POLL() cdl_poll()")
        if config.breakpoints == "software":
            lines.append("void cdl_bp(uns8 id);")
            lines.append("#define CDL_BP(id) cdl_bp(id)")
        else:
            lines.append("#define CDL_BP(id)")
    else:
        lines.append("#define CDL_POLL()")
    if decision.tier == "toggle":
        lines.append("void cdl_mark(uns8 id);")
        lines.append("#define CDL_MARK(id) cdl_mark(id)")
    lines.append("")
    lines.append(f"#endif // {guard}")
    return "\n".join(lines) + "\n"


_CRC_LINES = [
    "static uns8 cdl_crc8(uns8 crc, uns8 b) {",
    "    uns8 i;",
    "    crc ^= b;",
    "    for (i = 0; i < 8; i++) {",
    "        if (crc & 0x80) crc = (crc << 1) ^ CDL_CRC_POLY;",
    "        else crc = crc << 1;",
    "    }",
    "    return crc;",
    "}",
]

_FRAMER_LINES = [
    "static void cdl_tx_raw(uns8 b) {",
    "    if (b == CDL_FLAG || b == CDL_ESC) {",
    "        cdl_putc(CDL_ESC);",
    "        cdl_putc(b ^ CDL_ESC_XOR);",
    "    } else {",
    "        cdl_putc(b);",
    "    }",
    "}",
    "",
    "static void cdl_send(uns8 type, uns8 *args, uns8 len) {",
    "    uns8 i;",
    "    uns8 crc;",
    "    cdl_putc(CDL_FLAG);",
    "    crc = cdl_crc8(0, type);",
    "    cdl_tx_raw(type);",
    "    crc = cdl_crc8(crc, cdl_seq);",
    "    cdl_tx_raw(cdl_seq);",
    "    crc = cdl_crc8(crc, len);",
    "    cdl_tx_raw(len);",
    "    for (i = 0; i < len; i++) {",
    "        crc = cdl_crc8(crc, args[i]);",
    "        cdl_tx_raw(args[i]);",
    "    }",
    "    cdl_tx_raw(crc);",
    "    cdl_putc(CDL_FLAG);",
    "    cdl_seq++;",
    "}",
    "",
    "static void cdl_hello(void) {",
    "    uns8 a[6];",
    "    a[0] = CDL_PROTO_MAJOR;",
    "    a[1] = CDL_DEVID_HI;",
    "    a[2] = CDL_DEVID_LO;",
    "    a[3] = CDL_CAPS;",
    "    a[4] = CDL_TIER;",
    "    a[5] = CDL_CH_COUNT;",
    "    cdl_send(CDL_T_HELLO, a, 6);",
    "}",
    "",
    "void cdl_trace(uns8 ch, uns16 value) {",
    "    uns8 a[3];",
    "    a[0] = ch;",
    "    a[1] = (uns8)value;",
    "    a[2] = (uns8)(value >> 8);",
    "    cdl_send(CDL_T_TRACE, a, 3);",
    "}",
    "",
]


def _render_source(
    metadata: DeviceMetadata, config: DebugConfig, caps: DeviceCaps, decision: TierDecision
) -> str:
    tier = decision.tier
    short = _short(metadata.device)
    lines: list[str] = [
        "// Generated by cc5x-helper debug-stub -- DO NOT EDIT.",
        f'#include "cdl_monitor_{short}.h"',
        "",
        "uns8 cdl_seq;",
        "",
    ]

    if tier == "toggle":
        return _render_toggle_source(lines, metadata, config)

    eu = caps.eusart
    if eu is None:  # defensive; validate_debug already rejects this for full/min
        raise ValueError(f"tier {tier!r} requires an EUSART that was not detected")

    lines += [
        "// Baud divisor is Fosc-dependent and NOT derivable from pack metadata; supply it",
        "// (manifest transport.brg) or define CDL_SPBRG_VALUE here from the datasheet.",
        "#ifndef CDL_SPBRG_VALUE",
    ]
    if config.brg is not None:
        lines.append(f"#define CDL_SPBRG_VALUE 0x{config.brg & 0xFF:02X}")
    else:
        lines.append('#error "define CDL_SPBRG_VALUE (SPBRG for your Fosc/baud, see EUSART baud table)"')
    lines.append("#endif")
    lines.append("")
    lines += _CRC_LINES + [""]
    lines += [
        "static void cdl_putc(uns8 b) {",
        f"    while (({eu.trmt_reg} & 0x{1 << eu.trmt_bit:02X}) == 0) ;  // wait TSR empty (TRMT)",
        f"    {eu.tx_data} = b;",
        "}",
        "",
    ]
    lines += _FRAMER_LINES

    # --- UART init ---
    init: list[str] = ["void cdl_init(void) {", "    cdl_seq = 0;"]
    if config.tx_pin:
        tris, bit = _pin_tris(config.tx_pin, metadata)
        # Precompute masks as literals: CC5X flags ~(1<<n)/(1<<7) as integer overflow.
        init.append(
            f"    {tris} = {tris} & 0x{0xFF & ~(1 << bit):02X};  "
            f"// {_safe_comment(config.tx_pin)} TX output"
        )
    if config.rx_pin and eu.rx_data is not None:
        rtris, rbit = _pin_tris(config.rx_pin, metadata)
        init.append(
            f"    {rtris} = {rtris} | 0x{1 << rbit:02X};  // {_safe_comment(config.rx_pin)} RX input"
        )
    init += [
        "    // NOTE: on PPS-capable parts, routing TX/RX to the chosen pins (the PPS output",
        "    // code is a datasheet value) is the application's responsibility.",
        f"    {eu.spbrg} = CDL_SPBRG_VALUE;",
    ]
    txsta_val = 1 << eu.txen_bit
    if config.brgh and eu.brgh_bit is not None:
        txsta_val |= 1 << eu.brgh_bit
    init.append(f"    {eu.txsta} = 0x{txsta_val:02X};  // async (SYNC=0), TX enabled")
    rcsta_val = 1 << eu.spen_bit
    # Continuous Receive Enable (async reception setup step 7, EUSART datasheet):
    # the receiver only runs with CREN=1. Gate it on the receive path itself, not
    # on rx_pin -- a full-tier stub advertises CAP_RX_COMMANDS and emits cdl_poll()
    # whenever has_rx holds, even with no explicit rx_pin (PPS routing is the app's
    # job, see note above), so CREN must follow has_rx or inbound commands never
    # reach the dispatcher.
    if eu.has_rx and eu.cren_bit is not None:
        rcsta_val |= 1 << eu.cren_bit
    init.append(f"    {eu.rcsta} = 0x{rcsta_val:02X};  // serial port enabled")
    init.append("    cdl_hello();")
    init.append("}")
    init.append("")
    lines += init

    lines += _render_rx_handler(config, eu)
    return "\n".join(lines) + "\n"


def _render_rx_handler(config: DebugConfig, eu: EusartRegs) -> list[str]:
    """Tier A receive + command dispatch (PING/READ_MEM/SET_BP/CLR_BP/CONTINUE)."""
    have_rx = eu.has_rx
    sw_bp = config.breakpoints == "software"
    lines: list[str] = [
        "#define CDL_RX_MAX 16",
        "static uns8 cdl_rx[CDL_RX_MAX + 1];",
        "static uns8 cdl_rxn;",
        "static bit cdl_inframe;",
        "static bit cdl_rxesc;",
    ]
    if sw_bp:
        lines += ["static uns8 cdl_bp_mask;", "static uns8 cdl_cont_id;"]
    lines += [""]
    if sw_bp:
        # CC5X cannot generate a variable-count shift (1 << id); build the mask with a
        # loop of single-bit shifts instead.
        lines += [
            "static uns8 cdl_bitmask(uns8 n) {",
            "    uns8 m, k;",
            "    m = 1;",
            "    for (k = 0; k < n; k++) m = m << 1;",
            "    return m;",
            "}",
            "",
        ]
    lines += [
        "static void cdl_ack(uns8 refseq) {",
        "    uns8 a[1];",
        "    a[0] = refseq;",            # ACK ARG = ref_seq only (cdl_proto ACK)
        "    cdl_send(CDL_T_ACK, a, 1);",
        "}",
        "",
        "static void cdl_nak(uns8 refseq, uns8 code) {",
        "    uns8 a[2];",
        "    a[0] = refseq;",
        "    a[1] = code;",              # NAK ARG = ref_seq + code (cdl_proto NAK)
        "    cdl_send(CDL_T_NAK, a, 2);",
        "}",
        "",
        "static void cdl_read_mem(uns8 lo, uns8 hi, uns8 n) {",
        "    uns8 a[2 + CDL_RX_MAX];",
        "    uns8 i;",
        "    if (n > CDL_RX_MAX) n = CDL_RX_MAX;",
        "    a[0] = lo;",
        "    a[1] = hi;",
        "    FSR0L = lo;",
        "    FSR0H = hi;",
        "    for (i = 0; i < n; i++) {",
        "        a[2 + i] = INDF0;",
        "        FSR0L++;",
        "        if (FSR0L == 0) FSR0H++;",
        "    }",
        "    cdl_send(CDL_T_MEM_DATA, a, 2 + n);",
        "}",
        "",
        "static void cdl_dispatch(void) {",
        "    uns8 type, seq, len, crc, i;",
        "    if (cdl_rxn < 4) return;            // TYPE SEQ LEN ... CRC",
        "    type = cdl_rx[0];",
        "    seq = cdl_rx[1];",
        "    len = cdl_rx[2];",
        "    if (len > (cdl_rxn - 4)) return;     // need TYPE SEQ LEN <len> CRC; cdl_rxn>=4 above",
        "    crc = cdl_crc8(0, type);",
        "    crc = cdl_crc8(crc, seq);",
        "    crc = cdl_crc8(crc, len);",
        "    for (i = 0; i < len; i++) crc = cdl_crc8(crc, cdl_rx[3 + i]);",
        "    if (crc != cdl_rx[3 + len]) return; // bad CRC -> drop",
        "    if (type == CDL_T_PING) {",
        "        cdl_ack(seq);",
        "    } else if (type == CDL_T_READ_MEM) {",
        "        if (len < 3) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }",
        "        cdl_read_mem(cdl_rx[3], cdl_rx[4], cdl_rx[5]);",
    ]
    if sw_bp:
        lines += [
            "    } else if (type == CDL_T_SET_BP) {",
            "        if (len < 1) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }",
            f"        if (cdl_rx[3] < {MAX_BP}) cdl_bp_mask |= cdl_bitmask(cdl_rx[3]);",
            "        cdl_ack(seq);",
            "    } else if (type == CDL_T_CLR_BP) {",
            "        if (len < 1) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }",
            f"        if (cdl_rx[3] < {MAX_BP}) cdl_bp_mask &= (cdl_bitmask(cdl_rx[3]) ^ 0xFF);",
            "        cdl_ack(seq);",
            "    } else if (type == CDL_T_CONTINUE) {",
            "        if (len < 1) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }",
            "        cdl_cont_id = cdl_rx[3];",
            "        cdl_ack(seq);",
        ]
    lines += [
        "    } else {",
        "        cdl_nak(seq, CDL_NAK_UNKNOWN_TYPE);",
        "    }",
        "}",
        "",
    ]
    if not have_rx:
        lines += [
            "void cdl_poll(void) {",
            "    // No EUSART RX register/flag in metadata -> inbound commands unavailable.",
            "}",
            "",
        ]
    else:
        lines += [
            "static void cdl_rx_byte(uns8 b) {",
            "    if (b == CDL_FLAG) {",
            "        if (cdl_inframe && cdl_rxn > 0) cdl_dispatch();",
            "        cdl_rxn = 0;",
            "        cdl_inframe = 1;",
            "        cdl_rxesc = 0;",
            "        return;",
            "    }",
            "    if (!cdl_inframe) return;",
            "    if (b == CDL_ESC) { cdl_rxesc = 1; return; }",
            "    if (cdl_rxesc) { b ^= CDL_ESC_XOR; cdl_rxesc = 0; }",
            "    if (cdl_rxn <= CDL_RX_MAX) cdl_rx[cdl_rxn++] = b;",
            "}",
            "",
            "void cdl_poll(void) {",
            f"    while ({eu.rcif_reg} & 0x{1 << eu.rcif_bit:02X}) cdl_rx_byte({eu.rx_data});",
            "}",
            "",
        ]
    if sw_bp:
        lines += [
            "void cdl_bp(uns8 id) {",
            "    uns8 a[1];",
            f"    if (id >= {MAX_BP}) return;",
            "    if ((cdl_bp_mask & cdl_bitmask(id)) == 0) return;",
            "    a[0] = id;",
            "    cdl_send(CDL_T_BP_HIT, a, 1);",
            "    cdl_cont_id = 0xFF;",
            "    while (cdl_cont_id != id) cdl_poll();",
            "}",
            "",
        ]
    return lines


def _render_toggle_source(lines: list[str], metadata: DeviceMetadata, config: DebugConfig) -> str:
    """Tier C: a single timed GPIO pulse per CDL_MARK (fixed-site default)."""
    pin = config.toggle_pins[0] if config.toggle_pins else (config.tx_pin or "RA0")
    m = _PIN_RE.match(pin.upper())
    if not m:
        raise ValueError(f"toggle pin {pin!r} is not a valid Rxn name")
    port_letter, bit = m.group(1), int(m.group(2))
    lat = f"LAT{port_letter}"
    tris = f"TRIS{port_letter}"
    sfr_names = {s.name for s in metadata.sfrs}
    out_reg = lat if lat in sfr_names else f"PORT{port_letter}"
    if out_reg not in sfr_names or tris not in sfr_names:
        raise ValueError(f"toggle pin {pin!r} needs {tris}/{out_reg} which are absent from metadata")
    lines += [
        "// Tier C: timestamping is done by the probe; the target only pulses a pin.",
        f"// Encoding: {config.toggle_encoding}",
        "",
        "void cdl_init(void) {",
        f"    {tris} = {tris} & 0x{0xFF & ~(1 << bit):02X};  // {_safe_comment(pin)} output",
        f"    {out_reg} = {out_reg} & 0x{0xFF & ~(1 << bit):02X};",
        "}",
        "",
        "void cdl_mark(uns8 id) {",
        "    // fixed-site: a single pulse; the site is identified by which pin it is on.",
        f"    {out_reg} = {out_reg} | 0x{1 << bit:02X};",
        "    nop();",
        "    nop();",
        f"    {out_reg} = {out_reg} & 0x{0xFF & ~(1 << bit):02X};",
        "}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedDebug:
    device: str
    tier: str
    monitor_h_name: str
    monitor_c_name: str
    map_name: str
    monitor_h: str
    monitor_c: str
    map_json: str
    decision: TierDecision
    caps: DeviceCaps
    capabilities_bits: int


def generate_debug_stub(metadata: DeviceMetadata, debug_payload: object) -> GeneratedDebug:
    """Generate the CDL stub + map for a device. Raises ValueError on invalid config/device."""
    config = parse_debug_config(debug_payload)
    caps = detect_caps(metadata)
    decision = select_tier(config, caps)
    errors = validate_debug(config, caps, decision)
    if errors:
        raise ValueError("invalid debug configuration:\n  - " + "\n  - ".join(errors))

    short = _short(metadata.device)
    return GeneratedDebug(
        device=metadata.device,
        tier=decision.tier,
        monitor_h_name=f"cdl_monitor_{short}.h",
        monitor_c_name=f"cdl_monitor_{short}.c",
        map_name=f"cdl_map_{short}.json",
        monitor_h=_render_header(metadata, config, caps, decision),
        monitor_c=_render_source(metadata, config, caps, decision),
        map_json=json.dumps(build_map(metadata, config, caps, decision), indent=2) + "\n",
        decision=decision,
        caps=caps,
        capabilities_bits=_capabilities(config, caps, decision),
    )
