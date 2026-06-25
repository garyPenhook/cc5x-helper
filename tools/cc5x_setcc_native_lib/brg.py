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

v0.1 emits an **8-bit** divisor (BRG16=0) to match the generated stub, choosing
BRGH to minimize the baud error; 16-bit BRG is a documented future enhancement.
The value always comes from this formula with the source URL recorded as
provenance, never a hand-entered guess.
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

# (BRGH, multiplier M) for the two 8-bit async modes. Order is irrelevant -- the
# minimum-error solution is selected regardless.
_ASYNC_8BIT: tuple[tuple[bool, int], ...] = ((False, 64), (True, 16))

SPBRG_8BIT_MAX = 0xFF


@dataclass(frozen=True)
class BrgSolution:
    """A resolved EUSART baud setting. ``spbrg`` is the 8-bit SPxBRGL value (n)."""

    spbrg: int
    brgh: bool
    brg16: bool          # always False in v0.1 (8-bit only)
    multiplier: int      # M in baud = Fosc / (M*(n+1))
    actual_baud: int     # the baud the divisor actually produces
    error_frac: float    # (actual - requested) / requested

    @property
    def error_pct(self) -> float:
        return self.error_frac * 100.0


def compute_brg(fosc_hz: int, baud: int) -> BrgSolution:
    """Best 8-bit EUSART SPBRG for ``fosc_hz``/``baud`` (BRG16=0), per the formula
    above. Tries BRGH=0 (÷64) and BRGH=1 (÷16) and returns the one with the
    smallest |error|. Raises ValueError if neither yields an in-range divisor
    (n must be 0..255): too-slow baud at a high Fosc needs the 16-bit BRG (not yet
    emitted) or a manually supplied ``transport.brg``."""
    if fosc_hz <= 0:
        raise ValueError(f"Fosc must be positive, got {fosc_hz}")
    if baud <= 0:
        raise ValueError(f"baud must be positive, got {baud}")

    best: BrgSolution | None = None
    for brgh, mult in _ASYNC_8BIT:
        # n that brings Fosc/(M*(n+1)) closest to baud, rounded to the nearest int.
        n = round(fosc_hz / (mult * baud)) - 1
        if not 0 <= n <= SPBRG_8BIT_MAX:
            continue
        actual = fosc_hz / (mult * (n + 1))
        err = (actual - baud) / baud
        cand = BrgSolution(spbrg=n, brgh=brgh, brg16=False, multiplier=mult,
                           actual_baud=round(actual), error_frac=err)
        if best is None or abs(cand.error_frac) < abs(best.error_frac):
            best = cand

    if best is None:
        raise ValueError(
            f"no 8-bit SPBRG fits Fosc={fosc_hz} Hz / baud={baud} (SPBRG would "
            f"exceed 255); use a lower Fosc, a higher baud, or set transport.brg "
            f"manually (16-bit BRG is not yet emitted)")
    return best
