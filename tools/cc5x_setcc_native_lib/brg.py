"""brg.py -- compute the EUSART SPBRG divisor from Fosc + baud (master-plan P3).

The baud divisor is a *runtime* fact (it depends on the oscillator frequency,
which is not in pack metadata), so debuggen never guesses it. This module derives
it from the EUSART Baud Rate Generator formula, taken from the Microchip product
documentation -- NOT from memory (fact-from-source policy).

Formula source (confirmed via the Microchip MCP, ``search_microchip_product_documents``):
"EUSART Baud Rate Generator (BRG)", PIC16F15213/14/23/24/43/44 Low Pin Count
Microcontrollers (covers the PIC16F15244). Asynchronous mode (SYNC=0):

    baud = Fosc / (M * (n + 1)),   n = SPxBRGH:SPxBRGL

      M = 64   when BRGH=0, BRG16=0   (8-bit)
      M = 16   when BRGH=1, BRG16=0   (8-bit)
      M = 16   when BRGH=0, BRG16=1   (16-bit)
      M =  4   when BRGH=1, BRG16=1   (16-bit)

``compute_brg`` returns the **8-bit** divisor (BRG16=0) by default, choosing BRGH
to minimize the baud error. Passing ``allow_brg16=True`` also searches the two
16-bit modes (n up to 65535, the SPxBRGH:SPxBRGL pair), used to reach slow baud
rates at fast oscillators that overflow the 8-bit divisor, or to cut the error
when 8-bit cannot. The caller escalates to 16-bit only when 8-bit cannot satisfy
the tolerance, so existing 8-bit results are unchanged. The value always comes
from this formula with the source URL recorded as provenance, never a guess.
"""
from __future__ import annotations

from dataclasses import dataclass

# Citable provenance for the formula above (recorded in the stub comment + cdl_map).
BRG_SOURCE = (
    "Microchip EUSART Baud Rate Generator (BRG), PIC16F15213/14/23/24/43/44 "
    "Low Pin Count Microcontrollers (covers PIC16F15244)"
)
BRG_SOURCE_URL = (
    "https://onlinedocs.microchip.com/oxy/"
    "GUID-E0612FCE-295D-4301-A564-3C4610F2330C-en-US-8/"
    "GUID-29A0EA99-2814-4D89-9117-3B86742F537F.html"
)

# EUSART async modes as (BRGH, multiplier M, BRG16, n_max). The 8-bit modes are
# tried first so that, on an exact-error tie with a 16-bit mode (the divisor is
# identical), the cheaper 8-bit form is kept -- compute_brg replaces only on a
# *strictly* smaller error. This also makes the allow_brg16=False path byte-for-byte
# identical to the original 8-bit-only behavior.
_ASYNC_8BIT: tuple[tuple[bool, int], ...] = ((False, 64), (True, 16))
_ASYNC_16BIT: tuple[tuple[bool, int], ...] = ((False, 16), (True, 4))

SPBRG_8BIT_MAX = 0xFF
SPBRG_16BIT_MAX = 0xFFFF


@dataclass(frozen=True)
class BrgSolution:
    """A resolved EUSART baud setting. ``spbrg`` is the divisor n: the 8-bit SPxBRGL
    value when ``brg16`` is False, or the full 16-bit SPxBRGH:SPxBRGL pair otherwise
    (split via the ``spbrgl``/``spbrgh`` byte properties)."""

    spbrg: int
    brgh: bool
    brg16: bool
    multiplier: int      # M in baud = Fosc / (M*(n+1))
    actual_baud: int     # the baud the divisor actually produces
    error_frac: float    # (actual - requested) / requested

    @property
    def error_pct(self) -> float:
        return self.error_frac * 100.0

    @property
    def spbrgl(self) -> int:
        """Low byte of n (the SPxBRGL register value)."""
        return self.spbrg & 0xFF

    @property
    def spbrgh(self) -> int:
        """High byte of n (the SPxBRGH register value); 0 in 8-bit mode."""
        return (self.spbrg >> 8) & 0xFF


def compute_brg(fosc_hz: int, baud: int, *, allow_brgh: bool = True,
                allow_brg16: bool = False) -> BrgSolution:
    """Best EUSART SPBRG divisor for ``fosc_hz``/``baud``, per the formula above.

    Tries BRGH=0 (÷64) and BRGH=1 (÷16) in 8-bit mode and returns the one with the
    smallest |error|. With ``allow_brg16=True`` it also tries the two 16-bit modes
    (BRGH=0 ÷16, BRGH=1 ÷4; n up to 65535), keeping the 8-bit form on an exact tie.

    Raises ValueError if no enabled mode yields an in-range divisor (n must be
    0..255 for 8-bit, 0..65535 for 16-bit): a too-slow baud at a high Fosc needs
    ``allow_brg16=True`` (if the device exposes a BRG16 bit) or a manual
    ``transport.brg``.

    ``allow_brgh=False`` restricts the search to the ÷64 (BRGH=0) modes, for devices
    whose metadata exposes no BRGH field: a BRGH=1 solution would otherwise be
    emitted as a bit the stub cannot set, silently running at ÷64 (wrong baud)."""
    if fosc_hz <= 0:
        raise ValueError(f"Fosc must be positive, got {fosc_hz}")
    if baud <= 0:
        raise ValueError(f"baud must be positive, got {baud}")

    # (brgh, mult, brg16, n_max), 8-bit modes first (see _ASYNC_8BIT comment).
    modes: list[tuple[bool, int, bool, int]] = [
        (brgh, mult, False, SPBRG_8BIT_MAX) for brgh, mult in _ASYNC_8BIT
    ]
    if allow_brg16:
        modes += [(brgh, mult, True, SPBRG_16BIT_MAX) for brgh, mult in _ASYNC_16BIT]
    if not allow_brgh:
        modes = [m for m in modes if not m[0]]

    best: BrgSolution | None = None
    for brgh, mult, brg16, n_max in modes:
        # n that brings Fosc/(M*(n+1)) closest to baud, rounded to the nearest int.
        n = round(fosc_hz / (mult * baud)) - 1
        if not 0 <= n <= n_max:
            continue
        actual = fosc_hz / (mult * (n + 1))
        err = (actual - baud) / baud
        cand = BrgSolution(spbrg=n, brgh=brgh, brg16=brg16, multiplier=mult,
                           actual_baud=round(actual), error_frac=err)
        if best is None or abs(cand.error_frac) < abs(best.error_frac):
            best = cand

    if best is None:
        extra = ("" if allow_brg16 else
                 " (the 16-bit BRG was not enabled; this device may expose no BRG16 bit)")
        raise ValueError(
            f"no SPBRG fits Fosc={fosc_hz} Hz / baud={baud}: the divisor would "
            f"exceed its range{extra}; use a lower Fosc, a higher baud, or set "
            f"transport.brg manually")
    return best
