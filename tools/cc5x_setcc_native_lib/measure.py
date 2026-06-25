"""measure.py -- parse CC5X build reports for the P2 compile-and-measure gate.

Pure parsers + value objects for the three CC5X output files the tier-confirmation
gate consumes (cc5x-debug-probe 02 §6, 03 §7):

  .occ  code + RAM occupation  -> OccReport   (does the stub fit flash/RAM?)
  .var  symbol -> address map  -> [VarSymbol] (fill cdl_map.symbols; verify addrs)
  .fcs  call tree / depth      -> FcsReport   (does call depth fit the HW stack?)

No compiler invocation here -- that stays in validate_generated_headers.py. These
parse the files CC5X writes (generated with -V for .var and -Q for .fcs; .occ is
default). Formats are CC5X 3.8; golden fixtures under tests/golden/measure/ are
real compiler output so the parsers are pinned to the actual format.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OccReport:
    """Parsed .occ occupation summary."""

    chip: str | None
    ram_used: int    # bytes of data RAM the build occupies
    ram_free: int    # bytes of data RAM left
    code_words: int  # total program-memory words used
    code_pct: int    # CC5X's reported % of flash used

    @property
    def ram_total(self) -> int:
        return self.ram_used + self.ram_free


@dataclass(frozen=True)
class VarSymbol:
    """One row of the .var variable list."""

    name: str
    cls: str           # P direct / L local / G global / E extern / R overlap / C const
    bank: str          # bank digit, or "-" for access/unbanked
    address: int       # byte address (the file-register address; bit offset split out)
    bit: int | None    # bit index for a bitfield, else None
    size: int          # bytes (0 for a bit)
    accesses: int      # number of direct accesses (#AC)


@dataclass(frozen=True)
class FcsReport:
    """Parsed .fcs call structure."""

    max_depth: int            # deepest call-stack level reached (L0 == depth 1)
    functions: tuple[str, ...]  # functions in call-tree order


_OCC_CHIP = re.compile(r"Chip\s*=\s*(\S+)")
_OCC_RAM = re.compile(r"RAM usage:\s*(\d+)\s*bytes\s*\(\s*(\d+)\s*local\s*\)\s*,\s*(\d+)\s*bytes free")
_OCC_TOTAL = re.compile(r"Total of\s+(\d+)\s+code words\s*\(\s*(\d+)\s*%\)")


def parse_occ(text: str) -> OccReport:
    """Parse a CC5X .occ report. Raises ValueError if the required summary lines
    (RAM usage, Total code words) are absent -- a failed/truncated compile."""
    chip: str | None = None
    ram_used = ram_free = code_words = code_pct = None
    for line in text.splitlines():
        m = _OCC_CHIP.search(line)
        if m:
            chip = m.group(1)
        m = _OCC_RAM.search(line)
        if m:
            ram_used, ram_free = int(m.group(1)), int(m.group(3))
        m = _OCC_TOTAL.search(line)
        if m:
            code_words, code_pct = int(m.group(1)), int(m.group(2))
    if code_words is None or code_pct is None:
        raise ValueError("no 'Total of N code words' line in .occ (compile failed?)")
    if ram_used is None or ram_free is None:
        raise ValueError("no 'RAM usage:' line in .occ (compile failed?)")
    return OccReport(chip, ram_used, ram_free, code_words, code_pct)


# A .var data row, e.g. " G  0  0x024     16  :  1:  cdl_ring" or a bit field
# " P  -  0x003.0    0  :  0:  Carry". Columns: class, bank, address[.bit], size, #AC, name.
_VAR_ROW = re.compile(
    r"^\s*([PLGERC])\s+(\S)\s+0x([0-9A-Fa-f]+)(?:\.(\d+))?\s+(\d+)\s*:\s*(\d+)\s*:\s*(\S+)")


def parse_var(text: str) -> list[VarSymbol]:
    """Parse a CC5X .var variable list into VarSymbol rows (header lines ignored)."""
    syms: list[VarSymbol] = []
    for line in text.splitlines():
        m = _VAR_ROW.match(line)
        if not m:
            continue
        cls, bank, addr_hex, bit, size, acc, name = m.groups()
        syms.append(VarSymbol(
            name=name,
            cls=cls,
            bank=bank,
            address=int(addr_hex, 16),
            bit=int(bit) if bit is not None else None,
            size=int(size),
            accesses=int(acc),
        ))
    return syms


_FCS_LEVEL = re.compile(r"^\s*L(\d+)\s+(\S+)")


def parse_fcs(text: str) -> FcsReport:
    """Parse a CC5X .fcs call structure. max_depth is the deepest L<N> level + 1
    (a top-level main with no calls is depth 1), i.e. the hardware-stack slots the
    monitor path consumes."""
    max_level = -1
    functions: list[str] = []
    for line in text.splitlines():
        m = _FCS_LEVEL.match(line)
        if m:
            max_level = max(max_level, int(m.group(1)))
            functions.append(m.group(2))
    return FcsReport(max_depth=max_level + 1 if max_level >= 0 else 0,
                     functions=tuple(functions))
