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
  ``Fosc``, which is *not* in pack metadata. It is **derived** from manifest
  ``transport.fosc`` + ``transport.baud`` via the datasheet EUSART BRG formula (see
  :mod:`brg`, sourced from the Microchip docs and recorded as provenance), or taken from
  an explicit ``transport.brg`` override -- never guessed. With neither, generation fails
  with a clear error instead of emitting an unusable stub.
* **CC5X-safe emission.** Pack-derived tokens pass through the same ``_safe_identifier`` /
  ``_safe_comment`` discipline as :mod:`headergen`.
* **Tier capability floors are design rules, not measured facts.** The final tier must be
  confirmed against a measured CC5X budget (``02-target-footprint.md`` §6); this module
  picks a *provisional* tier and says so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
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
from . import brg as brgmod
from .cdl_protogen import render_proto_defines
from .headergen import _safe_comment, _safe_identifier
from .measure import FcsReport, OccReport, VarSymbol
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
_EUSART_SPBRGH = ("SPBRGH", "SP1BRGH", "SPBRGH1")
_EUSART_BAUDCON = ("BAUDCON", "BAUD1CON", "BAUDCON1", "BAUD1CON1")

_BIT_SPEN = ("SPEN",)
_BIT_BRG16 = ("BRG16", "BRG161")
_BIT_TXEN = ("TXEN", "TXEN1")
_BIT_SYNC = ("SYNC", "SYNC1")
_BIT_BRGH = ("BRGH", "BRGH1")
_BIT_CREN = ("CREN", "CREN1")
_BIT_OERR = ("OERR", "OERR1")
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
    fosc: int | None  # oscillator Hz; with baud, codegen derives SPBRG (P3, brg.py)
    brg: int | None  # explicit SPBRG value (override; bypasses the Fosc computation)
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
        fosc=_as_int(transport.get("fosc"), "transport.fosc"),
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
    oerr_bit: int | None      # Overrun Error bit in rcsta; recovered by toggling CREN
    trmt_reg: str
    trmt_bit: int
    rcif_reg: str | None
    rcif_bit: int | None
    spbrgh: str | None        # high byte of the 16-bit divisor (SPxBRGH)
    baudcon: str | None       # register holding the BRG16 select bit
    brg16_bit: int | None     # bit position of BRG16 within baudcon

    @property
    def has_brg16(self) -> bool:
        """True when the metadata exposes everything needed to emit a 16-bit BRG:
        the SPxBRGH high byte plus the BRG16 select bit (and its register). Without
        all three the stub cannot select 16-bit mode, so codegen stays 8-bit."""
        return (
            self.spbrgh is not None
            and self.baudcon is not None
            and self.brg16_bit is not None
        )

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


def _writable_ranges(metadata: DeviceMetadata) -> list[tuple[int, int, int]]:
    """Per-(hi-page) writable GPR sub-ranges for WRITE_MEM, as ``(page, lo, hi_lo)``:
    an address A is writable iff ``(A >> 8) == page`` and ``lo <= (A & 0xFF) <= hi_lo``.

    Whitelist = device GPR data RAM (RAMBANK + COMMON, from pack metadata) minus the
    ICD-reserved RAM. This refuses SFRs, config/calibration words and unimplemented
    space by construction (01 SS 6 write blacklist). Ranges are split at 256-byte
    boundaries so the generated stub compares ``hi``/``lo`` as plain uns8.

    Caveat: the stub's own GPR state (rx ring, SEQ, bp mask) lives at link-time
    addresses inside this same GPR space and cannot be excluded at codegen, so an
    enabled WRITE_MEM still lets a host corrupt the stub. WRITE_MEM stays off by
    default (01 SS 6) for exactly this reason.
    """
    allow: set[int] = set()
    for r in [*metadata.ram_ranges, *metadata.common_ranges]:
        if r.end >= r.start:
            allow.update(range(r.start, r.end + 1))
    for r in metadata.icd_ram_ranges:
        if r.end >= r.start:
            allow.difference_update(range(r.start, r.end + 1))
    if not allow:
        return []
    out: list[tuple[int, int, int]] = []
    addrs = sorted(allow)
    start = prev = addrs[0]
    for a in addrs[1:]:
        if a == prev + 1 and (a >> 8) == (start >> 8):
            prev = a
            continue
        out.append((start >> 8, start & 0xFF, prev & 0xFF))
        start = prev = a
    out.append((start >> 8, start & 0xFF, prev & 0xFF))
    return out


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
    spbrgh = _first_present(_EUSART_SPBRGH, sfr_names)
    baudcon = _first_present(_EUSART_BAUDCON, sfr_names)
    rx_data = _first_present(_EUSART_RX_DATA, sfr_names)

    _, spen = _field(_BIT_SPEN, field_info)
    _, txen = _field(_BIT_TXEN, field_info)
    _, sync = _field(_BIT_SYNC, field_info)
    _, brgh = _field(_BIT_BRGH, field_info)
    brg16_reg, brg16 = _field(_BIT_BRG16, field_info)
    _, cren = _field(_BIT_CREN, field_info)
    _, oerr = _field(_BIT_OERR, field_info)
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
            oerr_bit=oerr,
            trmt_reg=trmt_reg,
            trmt_bit=trmt,
            rcif_reg=rcif_reg,
            rcif_bit=rcif,
            spbrgh=spbrgh,
            # The register that actually contains BRG16 is authoritative (mirrors
            # trmt_reg/rcif_reg); fall back to the name-detected BAUDCON only if the
            # field had no resolvable container.
            baudcon=brg16_reg or baudcon,
            brg16_bit=brg16,
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


# Tighter than the ~3% the async EUSART tolerates, to leave headroom for clock drift.
MAX_BAUD_ERROR_FRAC = 0.025


@dataclass(frozen=True)
class BrgResolution:
    """The effective EUSART baud setting (P3): either derived from Fosc+baud via
    :mod:`brg` (the formula's provenance is recorded), or a manual ``transport.brg``
    override. ``actual_baud``/``error_pct`` are populated only when computed."""

    spbrg: int           # divisor n: 8-bit (brg16=False) or full 16-bit pair
    spbrgh: int          # high byte of n; 0 unless brg16
    brgh: bool
    brg16: bool
    computed: bool
    fosc: int | None
    baud: int | None
    actual_baud: int | None
    error_pct: float | None
    source: str | None
    source_url: str | None


def resolve_brg(config: DebugConfig, *, brgh_available: bool = True,
                brg16_available: bool = False) -> BrgResolution | None:
    """Resolve the SPBRG/BRGH the stub should use, or None if the manifest gives no
    baud information. A manual ``transport.brg`` wins (override, byte-for-byte the
    old 8-bit behavior); otherwise ``transport.fosc`` + ``transport.baud`` drive the
    EUSART BRG formula (brg.py, datasheet-sourced). Raises ValueError when a
    requested Fosc/baud cannot be hit within tolerance by the available BRG.

    The computation is tried 8-bit first; only if that overflows or exceeds the
    tolerance, and ``brg16_available`` holds, is the 16-bit BRG searched. Existing
    8-bit-reachable rates therefore resolve byte-identically.

    ``brgh_available`` is whether the device exposes a BRGH bit; when False the
    computation is restricted to BRGH=0 (÷64), so a BRGH=1 divisor is never emitted
    as a bit the stub cannot set (which would silently run at the wrong baud)."""
    if config.brg is not None:
        return BrgResolution(spbrg=config.brg & 0xFF, spbrgh=0, brgh=config.brgh,
                             brg16=False, computed=False, fosc=config.fosc,
                             baud=config.baud, actual_baud=None, error_pct=None,
                             source="manual transport.brg override", source_url=None)
    if config.fosc is not None and config.baud is not None:
        # 8-bit first: keeps already-reachable rates byte-identical. Escalate to the
        # 16-bit BRG only as a rescue (overflow or out-of-tolerance) when supported.
        try:
            sol = brgmod.compute_brg(config.fosc, config.baud, allow_brgh=brgh_available)
        except ValueError:
            sol = None
        if (sol is None or abs(sol.error_frac) > MAX_BAUD_ERROR_FRAC) and brg16_available:
            sol = brgmod.compute_brg(config.fosc, config.baud, allow_brgh=brgh_available,
                                     allow_brg16=True)
        if sol is None:
            raise ValueError(
                f"no in-range SPBRG for Fosc={config.fosc} Hz / baud={config.baud}; "
                f"use a lower Fosc, a higher baud, or set transport.brg manually")
        if abs(sol.error_frac) > MAX_BAUD_ERROR_FRAC:
            hint = ("choose a UART-friendly Fosc or set transport.brg manually"
                    if brgh_available else
                    "this device exposes no BRGH bit (÷16 unavailable); pick a Fosc that "
                    "hits the baud in ÷64 mode or set transport.brg manually")
            mode = "16-bit" if sol.brg16 else "8-bit"
            raise ValueError(
                f"computed baud error {sol.error_pct:.2f}% for Fosc={config.fosc} Hz / "
                f"baud={config.baud} exceeds {MAX_BAUD_ERROR_FRAC * 100:.1f}% on the "
                f"{mode} BRG; {hint}")
        return BrgResolution(spbrg=sol.spbrg, spbrgh=sol.spbrgh, brgh=sol.brgh,
                             brg16=sol.brg16, computed=True, fosc=config.fosc,
                             baud=config.baud, actual_baud=sol.actual_baud,
                             error_pct=round(sol.error_pct, 2),
                             source=brgmod.BRG_SOURCE, source_url=brgmod.BRG_SOURCE_URL)
    return None


def _brgh_available(caps: DeviceCaps) -> bool:
    """Whether the device's EUSART exposes a BRGH field (so the ÷16 mode is usable)."""
    return caps.eusart is not None and caps.eusart.brgh_bit is not None


def _brg16_available(caps: DeviceCaps) -> bool:
    """Whether the device's EUSART exposes the BRG16 select bit + SPxBRGH high byte,
    so the 16-bit BRG can be emitted."""
    return caps.eusart is not None and caps.eusart.has_brg16


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
        try:
            if resolve_brg(config, brgh_available=_brgh_available(caps),
                           brg16_available=_brg16_available(caps)) is None:
                errors.append(
                    "tier needs an EUSART baud divisor: set debug.transport.fosc (oscillator "
                    "Hz) + debug.transport.baud so codegen derives SPBRG from the datasheet "
                    "formula, or set debug.transport.brg to an explicit SPBRG value"
                )
        except ValueError as exc:
            errors.append(str(exc))
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
        # Every capability below is a host->target command serviced by the RX
        # dispatcher. Without a usable receive path the stub emits only the empty
        # cdl_poll() stub, so advertising any of them would make the host issue
        # commands that are silently never dispatched. Gate them all on has_rx --
        # a full/min device whose EUSART has TX but no RCREG/RCIF degrades to a
        # trace-only build that advertises no commands.
        if caps.eusart is not None and caps.eusart.has_rx:
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
    # Baud provenance (P3): the SPBRG the stub uses, derived from Fosc+baud with a
    # citable datasheet source, or a manual override. Absent if the manifest gives
    # no baud info (e.g. trace/toggle tiers, or a config still to be filled in).
    try:
        resolved = resolve_brg(config, brgh_available=_brgh_available(caps),
                               brg16_available=_brg16_available(caps))
    except ValueError:
        resolved = None
    if resolved is not None:
        payload["baud"] = {
            # spbrg/spbrgh are the SPxBRGL/SPxBRGH *register bytes* (spbrgh=0 and the
            # divisor fits spbrg alone in 8-bit mode), so a consumer loads them
            # directly without re-splitting. divisor_n carries the full value for
            # display. Keep these two as bytes -- do not pair them as (spbrgh<<8)|spbrg
            # with a >8-bit spbrg.
            "spbrg": resolved.spbrg & 0xFF,
            "spbrgh": resolved.spbrgh,
            "divisor_n": resolved.spbrg,
            "brgh": resolved.brgh,
            "brg16": resolved.brg16,
            "computed": resolved.computed,
            "fosc_hz": resolved.fosc,
            "requested": resolved.baud,
            "actual": resolved.actual_baud,
            "error_pct": resolved.error_pct,
            "source": resolved.source,
            "source_url": resolved.source_url,
        }
    if decision.tier == "toggle":
        payload["toggle"] = {
            "encoding": config.toggle_encoding,
            "pins": list(config.toggle_pins),
            "sites": [{"id": ch.id, "name": ch.name} for ch in config.channels],
        }
    return payload


# --------------------------------------------------------------------------------------
# Measured-budget gate (P2): confirm or demote the provisional tier from real CC5X
# reports, and fill the map's symbols/budget. 02 §6 acceptance gate. The reports are
# parsed by :mod:`measure`; nothing here invokes the compiler (that is the CrossOver
# runner in validate_generated_headers.py).
# --------------------------------------------------------------------------------------

# Doc 03 §3.3 design rule: keep >= 50% of data RAM for the application. Configurable.
DEFAULT_APP_HEADROOM_FRAC = 0.5


def _next_lower_tier(tier: str) -> str:
    """The next tier down the full -> min -> trace -> toggle ladder (02 §6), floored
    at 'toggle'."""
    return TIER_ORDER[max(0, TIER_ORDER.index(tier) - 1)]


def confirm_tier(
    decision: TierDecision,
    caps: DeviceCaps,
    occ: OccReport,
    *,
    fcs: FcsReport | None = None,
    hw_stack_depth: int | None = None,
    app_headroom_frac: float = DEFAULT_APP_HEADROOM_FRAC,
) -> TierDecision:
    """Confirm or demote the provisional tier against a *measured* CC5X budget.

    The provisional tier (from :func:`select_tier`) is a hypothesis; the measured
    stub is the proof (02 §6). The ``.occ`` for the stub compiled *at the provisional
    tier* must fit:

      * **RAM** -- the stub leaves at least ``app_headroom_frac`` of device RAM for
        the application (03 §3.3 keeps >= 50% for the app);
      * **flash** -- stub code words fit the device's program memory;
      * **stack** -- (optional) the monitor call depth fits ``hw_stack_depth``. The
        hardware stack depth is a per-device fact the caller must supply (from pack
        metadata / the Microchip MCP); it is never assumed here, so when it is not
        given the stack check is skipped.

    On success the tier is **confirmed** (``provisional=False``). On failure it is
    **demoted one step** and stays provisional -- the lower tier's lighter stub has
    not itself been measured, so the caller regenerates at the new tier and re-runs
    the gate (02 §6: "downgrades ... and records why").
    """
    ram_reserve = int(round(occ.ram_total * app_headroom_frac))
    ram_budget = occ.ram_total - ram_reserve            # max RAM the stub may use
    ram_ok = occ.ram_used <= ram_budget
    flash_ok = occ.code_words <= caps.flash_words
    pct = int(round(app_headroom_frac * 100))

    # Stack check is opt-in: skipped unless an explicit per-device depth is given
    # (narrowed here so the call-depth comparison sees two ints, not int|None).
    stack_ok = True
    stack_detail: str | None = None
    if fcs is not None and hw_stack_depth is not None:
        stack_ok = fcs.max_depth <= hw_stack_depth
        stack_detail = f"stack {fcs.max_depth}{'<=' if stack_ok else '>'}{hw_stack_depth}"

    if ram_ok and flash_ok and stack_ok:
        bits = [f"RAM {occ.ram_used}B<={ram_budget}B ({pct}% app reserve)",
                f"code {occ.code_words}w<={caps.flash_words}w flash"]
        if stack_detail is not None:
            bits.append(stack_detail)
        return TierDecision(tier=decision.tier, provisional=False, forced=decision.forced,
                            reason="measured: " + ", ".join(bits))

    fails: list[str] = []
    if not ram_ok:
        fails.append(f"RAM {occ.ram_used}B>{ram_budget}B ({pct}% app reserve)")
    if not flash_ok:
        fails.append(f"code {occ.code_words}w>{caps.flash_words}w flash")
    if not stack_ok and stack_detail is not None:
        fails.append(stack_detail)
    lower = _next_lower_tier(decision.tier)
    reason = f"measured demote {decision.tier!r}->{lower!r}: " + "; ".join(fails) + \
             f"; re-measure at {lower!r}"
    return TierDecision(tier=lower, provisional=True, forced=decision.forced, reason=reason)


def symbols_from_var(varsyms: list[VarSymbol]) -> dict:
    """Build the map's ``symbols`` table from a parsed ``.var``: name -> file-register
    address (+ bank/size/class), so the PC tool resolves a name to an address for
    READ_MEM (01 §6, 03 §3.6). Bitfields carry their bit index; multi-byte vars carry
    their size. Populating from ``.var`` *is* the 02 §6 "symbol exists at the claimed
    address" check -- the addresses come straight from the compiler, not a guess."""
    out: dict[str, dict] = {}
    for s in varsyms:
        entry: dict[str, object] = {"address": s.address, "bank": s.bank,
                                    "size": s.size, "class": s.cls}
        if s.bit is not None:
            entry["bit"] = s.bit
        out[s.name] = entry
    return out


def budget_from_occ(occ: OccReport) -> dict:
    """The map's ``budget`` block, filled from a measured ``.occ`` (02 §6)."""
    return {
        "measured": True,
        "chip": occ.chip,
        "ram_used": occ.ram_used,
        "ram_free": occ.ram_free,
        "ram_total": occ.ram_total,
        "code_words": occ.code_words,
        "code_pct": occ.code_pct,
    }


def apply_measurement(
    map_payload: dict, decision: TierDecision, occ: OccReport, varsyms: list[VarSymbol]
) -> dict:
    """Fold measured results into a :func:`build_map` payload (mutated in place and
    returned): the confirmed/demoted tier fields, the ``symbols`` table (from
    ``.var``), and the ``budget`` block (from ``.occ``). ``decision`` must be the
    output of :func:`confirm_tier` (i.e. already confirmed or demoted)."""
    map_payload["tier"] = decision.tier
    map_payload["tier_wire"] = TIER_WIRE.get(decision.tier, 0)
    map_payload["tier_provisional"] = decision.provisional
    map_payload["tier_forced"] = decision.forced
    map_payload["tier_reason"] = decision.reason
    map_payload["symbols"] = symbols_from_var(varsyms)
    map_payload["budget"] = budget_from_occ(occ)
    return map_payload


# One compiled+parsed measurement of the stub at a given tier: the (.occ, .var, .fcs)
# reports for that tier's stub. ``fcs`` is None when the call structure was not
# requested/produced. Returned by a ``MeasureFn`` so the gate can re-measure on demote.
Measurement = tuple["OccReport", "list[VarSymbol]", "FcsReport | None"]
# Compile the stub *at this tier* and return its parsed reports. Injected into
# :func:`run_measure_gate` so the gate loop is pure/testable; the real implementation
# (CrossOver CC5X compile + measure.parse_*) lives in validate_generated_headers.py.
MeasureFn = Callable[[str], Measurement]


@dataclass(frozen=True)
class GateResult:
    """Outcome of the 02 §6 measured-budget gate (see :func:`run_measure_gate`)."""

    decision: TierDecision            # final tier; provisional=False iff measurement-confirmed
    occ: OccReport                    # the measurement at ``decision.tier`` ...
    varsyms: list[VarSymbol]          # ... so apply_measurement fills the map from matching data
    fcs: FcsReport | None
    history: tuple[str, ...]          # one reason per attempt, oldest first (02 §6 "records why")

    @property
    def confirmed(self) -> bool:
        return not self.decision.provisional


def run_measure_gate(
    decision: TierDecision,
    caps: DeviceCaps,
    measure: MeasureFn,
    *,
    hw_stack_depth: int | None = None,
    app_headroom_frac: float = DEFAULT_APP_HEADROOM_FRAC,
) -> GateResult:
    """Run the 02 §6 acceptance gate: measure the stub at the provisional tier, then
    confirm it or demote-and-re-measure down the full->min->trace->toggle ladder until
    a tier's measured budget fits or the 'toggle' floor is reached.

    ``decision`` is the provisional pick from :func:`select_tier`. ``measure(tier)``
    compiles the stub *regenerated at that tier* and returns its parsed reports; it is
    called once per tier tried (so a demote re-measures the lighter stub, per
    :func:`confirm_tier`). The returned :class:`GateResult` carries the measurement that
    matches the final tier, so the caller can ``apply_measurement`` with consistent data.

    A confirmed result has ``decision.provisional is False``. If even the 'toggle' floor
    fails to fit, the result stays provisional (``confirmed`` False) with the floor's
    measurement and reason -- an honest gate failure the caller/CI must surface, not a
    silent pass. The loop is bounded by the ladder length so a misbehaving ``measure``
    cannot spin forever.
    """
    current = decision
    history: list[str] = []
    # At most one attempt per rung of the ladder; the floor (toggle->toggle) also breaks
    # below, this bound is the belt-and-suspenders backstop.
    for _ in range(len(TIER_ORDER)):
        occ, varsyms, fcs = measure(current.tier)
        result = confirm_tier(
            current, caps, occ,
            fcs=fcs, hw_stack_depth=hw_stack_depth, app_headroom_frac=app_headroom_frac,
        )
        history.append(result.reason)
        if not result.provisional:                      # measurement-confirmed
            return GateResult(result, occ, varsyms, fcs, tuple(history))
        if result.tier == current.tier:                 # floored at 'toggle', cannot demote
            return GateResult(result, occ, varsyms, fcs, tuple(history))
        current = result                                # demoted; re-measure lighter stub
    # Unreachable in practice (the floor breaks first); return the last state honestly.
    return GateResult(current, occ, varsyms, fcs, tuple(history))


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
    "    a[1] = CDL_DEVID_LO;",   # devid is U16LE on the wire (cdl_proto): low byte first
    "    a[2] = CDL_DEVID_HI;",
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

    resolved = resolve_brg(config, brgh_available=_brgh_available(caps),
                           brg16_available=_brg16_available(caps))
    if resolved is not None and resolved.computed:
        # SPBRG derived from Fosc+baud via the datasheet EUSART BRG formula (P3).
        mode = "16-bit BRG16=1" if resolved.brg16 else "8-bit"
        lines += [
            f"// SPBRG computed from Fosc+baud via the EUSART BRG formula ({resolved.source}).",
            f"// Fosc={resolved.fosc} Hz, baud={resolved.baud} -> SPBRG={resolved.spbrg} "
            f"({mode}), BRGH={int(resolved.brgh)} (actual {resolved.actual_baud} baud, "
            f"{resolved.error_pct:+.2f}% error).",
            f"// Source: {resolved.source_url}",
            "#ifndef CDL_SPBRG_VALUE",
            f"#define CDL_SPBRG_VALUE 0x{resolved.spbrg & 0xFF:02X}",
            "#endif",
        ]
        if resolved.brg16:
            lines += [
                "#ifndef CDL_SPBRGH_VALUE",
                f"#define CDL_SPBRGH_VALUE 0x{resolved.spbrgh:02X}",
                "#endif",
            ]
    else:
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
    ]
    if resolved is not None and resolved.brg16 and eu.has_brg16:
        # 16-bit BRG: select BRG16 and load the high byte before the low byte. Per the
        # EUSART datasheet, writing SPxBRGL resets the BRG timer, so it must come last.
        init += [
            f"    {eu.baudcon} = 0x{1 << eu.brg16_bit:02X};  // BRG16=1 (16-bit BRG)",
            f"    {eu.spbrgh} = CDL_SPBRGH_VALUE;",
        ]
    init.append(f"    {eu.spbrg} = CDL_SPBRG_VALUE;")
    txsta_val = 1 << eu.txen_bit
    # BRGH from the resolved baud setting: the computed divisor picks BRGH itself
    # (manual transport.brg keeps config.brgh).
    brgh_on = resolved.brgh if resolved is not None else config.brgh
    if brgh_on and eu.brgh_bit is not None:
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

    lines += _render_rx_handler(config, eu, metadata)
    return "\n".join(lines) + "\n"


def _render_rx_handler(config: DebugConfig, eu: EusartRegs, metadata: DeviceMetadata) -> list[str]:
    """Tier A receive + command dispatch (PING/READ_MEM/WRITE_MEM/SET_BP/CLR_BP/CONTINUE)."""
    have_rx = eu.has_rx
    sw_bp = config.breakpoints == "software"
    write_mem = config.write_mem
    writable = _writable_ranges(metadata) if write_mem else []
    # cdl_rx holds a full de-framed packet: TYPE SEQ LEN <args> CRC. CDL_RX_MAX is the
    # data-payload cap (READ_MEM bytes returned / WRITE_MEM bytes accepted); the frame
    # buffer must be sized separately, since the widest received frame is a max WRITE_MEM:
    # TYPE SEQ LEN addr_lo addr_hi + CDL_RX_MAX data + CRC = 6 + CDL_RX_MAX. Conflating the
    # two (cdl_rx[CDL_RX_MAX + 1]) truncated near-max writes before dispatch. Without
    # write_mem the widest frame is READ_MEM (TYPE SEQ LEN lo hi count CRC = 7).
    frame_max = "(6 + CDL_RX_MAX)" if write_mem else "7"
    lines: list[str] = [
        "#define CDL_RX_MAX 16",
        f"#define CDL_FRAME_MAX {frame_max}",
        "static uns8 cdl_rx[CDL_FRAME_MAX];",
        "static uns8 cdl_rxn;",
        "static bit cdl_inframe;",
        "static bit cdl_rxesc;",
        # Invalid escape seen -> drop the whole frame (mirrors cdl_codec's _bad flag).
        "static bit cdl_rxbad;",
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
    ]
    if write_mem:
        # WRITE_MEM whitelist: only addresses inside the device's GPR data RAM are
        # writable (01 SS 6); SFRs/config/ICD-RAM/unimplemented space are NAK'd
        # WRITE_DENIED. Verify the whole range before touching memory so a partly
        # out-of-range write performs no partial write. See _writable_ranges for the
        # stub-self-state caveat.
        check = [
            "static uns8 cdl_addr_writable(uns8 lo, uns8 hi) {",
        ]
        if writable:
            for page, lo, hi_lo in writable:
                check.append(
                    f"    if (hi == 0x{page:02X} && lo >= 0x{lo:02X} && lo <= 0x{hi_lo:02X}) return 1;"
                )
        check.append("    return 0;")
        check.append("}")
        lines += check
        lines += [
            "",
            "static void cdl_write_mem(uns8 refseq, uns8 lo, uns8 hi, uns8 n) {",
            "    uns8 i, l, h;",
            "    if (n > CDL_RX_MAX) { cdl_nak(refseq, CDL_NAK_BAD_LEN); return; }",
            "    l = lo;",
            "    h = hi;",
            "    for (i = 0; i < n; i++) {           // refuse the whole frame if any byte is off-limits",
            "        if (cdl_addr_writable(l, h) == 0) { cdl_nak(refseq, CDL_NAK_WRITE_DENIED); return; }",
            "        l++;",
            "        if (l == 0) h++;",
            "    }",
            "    FSR0L = lo;",
            "    FSR0H = hi;",
            "    for (i = 0; i < n; i++) {",
            "        INDF0 = cdl_rx[5 + i];",
            "        FSR0L++;",
            "        if (FSR0L == 0) FSR0H++;",
            "    }",
            "    cdl_ack(refseq);",
            "}",
            "",
        ]
    lines += [
        "static void cdl_dispatch(void) {",
        "    uns8 type, seq, len, crc, i;",
        "    if (cdl_rxn < 4) return;            // TYPE SEQ LEN ... CRC",
        "    type = cdl_rx[0];",
        "    seq = cdl_rx[1];",
        "    len = cdl_rx[2];",
        "    if (len != (cdl_rxn - 4)) return;    // exact len: TYPE SEQ LEN <len> CRC, no trailing bytes",
        "    crc = cdl_crc8(0, type);",
        "    crc = cdl_crc8(crc, seq);",
        "    crc = cdl_crc8(crc, len);",
        "    for (i = 0; i < len; i++) crc = cdl_crc8(crc, cdl_rx[3 + i]);",
        "    if (crc != cdl_rx[3 + len]) return; // bad CRC -> drop",
        "    if (type == CDL_T_PING) {",
        "        if (len != 0) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }  // PING carries no args",
        "        cdl_ack(seq);",
        "    } else if (type == CDL_T_READ_MEM) {",
        "        if (len != 3) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }  // exact: addr_lo addr_hi count",
        "        cdl_read_mem(cdl_rx[3], cdl_rx[4], cdl_rx[5]);",
    ]
    if write_mem:
        lines += [
            "    } else if (type == CDL_T_WRITE_MEM) {",
            "        if (len < 3) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }  // addr_lo addr_hi + >=1 byte",
            "        cdl_write_mem(seq, cdl_rx[3], cdl_rx[4], len - 2);",
        ]
    if sw_bp:
        lines += [
            "    } else if (type == CDL_T_SET_BP) {",
            "        if (len != 1) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }  // exact: one bp_id",
            f"        if (cdl_rx[3] >= {MAX_BP}) {{ cdl_nak(seq, CDL_NAK_BAD_BP); return; }}  // don't ACK a BP that can never fire",
            "        cdl_bp_mask |= cdl_bitmask(cdl_rx[3]);",
            "        cdl_ack(seq);",
            "    } else if (type == CDL_T_CLR_BP) {",
            "        if (len != 1) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }  // exact: one bp_id",
            f"        if (cdl_rx[3] >= {MAX_BP}) {{ cdl_nak(seq, CDL_NAK_BAD_BP); return; }}",
            "        cdl_bp_mask &= (cdl_bitmask(cdl_rx[3]) ^ 0xFF);",
            "        cdl_ack(seq);",
            "    } else if (type == CDL_T_CONTINUE) {",
            "        if (len != 1) { cdl_nak(seq, CDL_NAK_BAD_LEN); return; }  // exact: one bp_id",
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
        # The 2-deep RX FIFO latches OERR if a 3rd byte arrives before it is drained,
        # and reception then halts until CREN is toggled (datasheet 24.4.1.7, confirmed
        # via Microchip MCP). Without this, any polling gap > ~2 char-times during inbound
        # traffic permanently jams the receiver -- no NAK, just silence. Drain first so the
        # two buffered bytes are still recovered, then clear OERR by toggling CREN.
        oerr_recovery: list[str] = []
        if eu.oerr_bit is not None and eu.cren_bit is not None:
            cren_mask = 1 << eu.cren_bit
            oerr_recovery = [
                f"    if ({eu.rcsta} & 0x{1 << eu.oerr_bit:02X}) {{           // RX overrun: re-enable receiver",
                f"        {eu.rcsta} &= 0x{(~cren_mask) & 0xFF:02X};",
                f"        {eu.rcsta} |= 0x{cren_mask:02X};",
                "    }",
            ]
        lines += [
            "static void cdl_rx_byte(uns8 b) {",
            "    if (b == CDL_FLAG) {",
            "        // Drop a frame poisoned by an invalid escape, or left with a dangling ESC at",
            "        // the closing FLAG, instead of dispatching corrupt bytes (mirrors cdl_codec).",
            "        if (cdl_inframe && cdl_rxn > 0 && !cdl_rxbad && !cdl_rxesc) cdl_dispatch();",
            "        cdl_rxn = 0;",
            "        cdl_inframe = 1;",
            "        cdl_rxesc = 0;",
            "        cdl_rxbad = 0;",
            "        return;",
            "    }",
            "    if (!cdl_inframe) return;",
            "    if (b == CDL_ESC) { cdl_rxesc = 1; return; }",
            "    if (cdl_rxesc) {",
            "        b ^= CDL_ESC_XOR;",
            "        cdl_rxesc = 0;",
            "        // Only FLAG/ESC are valid escaped bytes; anything else is malformed wire.",
            "        if (b != CDL_FLAG && b != CDL_ESC) { cdl_rxbad = 1; return; }",
            "    }",
            "    if (cdl_rxn < CDL_FRAME_MAX) cdl_rx[cdl_rxn++] = b;",
            "}",
            "",
            "void cdl_poll(void) {",
            f"    while ({eu.rcif_reg} & 0x{1 << eu.rcif_bit:02X}) cdl_rx_byte({eu.rx_data});",
            *oerr_recovery,
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
    """Tier C: a single timed GPIO pulse per CDL_MARK (fixed-site default).

    Fixed-site encoding gives each marked site its own pin (validation enforces one
    pin per channel), and cdl_mark(id) pulses the pin owned by site `id` so the probe
    can tell sites apart. With one pin (or none) the body is a single branchless pulse;
    with several it dispatches on `id`. With no configured pins, fall back to one
    default pin.
    """
    pins = list(config.toggle_pins) or [config.tx_pin or "RA0"]
    sfr_names = {s.name for s in metadata.sfrs}
    sites: list[tuple[str, str, int, str]] = []  # (out_reg, tris, bit, pin); index == marker id
    for pin in pins:
        m = _PIN_RE.match(pin.upper())
        if not m:
            raise ValueError(f"toggle pin {pin!r} is not a valid Rxn name")
        port_letter, bit = m.group(1), int(m.group(2))
        lat = f"LAT{port_letter}"
        tris = f"TRIS{port_letter}"
        out_reg = lat if lat in sfr_names else f"PORT{port_letter}"
        if out_reg not in sfr_names or tris not in sfr_names:
            raise ValueError(f"toggle pin {pin!r} needs {tris}/{out_reg} which are absent from metadata")
        sites.append((out_reg, tris, bit, pin))

    lines += [
        "// Tier C: timestamping is done by the probe; the target only pulses a pin.",
        f"// Encoding: {config.toggle_encoding}",
        "",
        "void cdl_init(void) {",
    ]
    for out_reg, tris, bit, pin in sites:
        lines += [
            f"    {tris} = {tris} & 0x{0xFF & ~(1 << bit):02X};  // {_safe_comment(pin)} output",
            f"    {out_reg} = {out_reg} & 0x{0xFF & ~(1 << bit):02X};",
        ]
    lines += ["}", "", "void cdl_mark(uns8 id) {"]
    if len(sites) == 1:
        out_reg, _tris, bit, _pin = sites[0]
        lines += [
            "    // fixed-site: a single pulse; the site is identified by which pin it is on.",
            f"    {out_reg} = {out_reg} | 0x{1 << bit:02X};",
            "    nop();",
            "    nop();",
            f"    {out_reg} = {out_reg} & 0x{0xFF & ~(1 << bit):02X};",
        ]
    else:
        lines.append("    // fixed-site: pulse the pin owned by site `id`.")
        for idx, (out_reg, _tris, bit, pin) in enumerate(sites):
            head = "    if" if idx == 0 else "    } else if"
            lines += [
                f"{head} (id == {idx}) {{          // {_safe_comment(pin)}",
                f"        {out_reg} = {out_reg} | 0x{1 << bit:02X};",
                "        nop();",
                "        nop();",
                f"        {out_reg} = {out_reg} & 0x{0xFF & ~(1 << bit):02X};",
            ]
        lines.append("    }")
    lines.append("}")
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
