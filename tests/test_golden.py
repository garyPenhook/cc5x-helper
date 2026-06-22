"""Golden (characterization) tests for the header + managed-config-block renderers.

These pin the exact text `render_full_header`, `render_dynamic_config_section`, and
`render_config_block_from_symbols` produce for a fixed, *synthetic* device so a future
change to the generators is a deliberate golden update rather than a silent drift.

The device metadata is built in-process (not read from Microchip packs), so these run
everywhere — including CI without packs installed — and are fully deterministic.

To refresh the golden fixtures after an intentional generator change:

    python tests/test_golden.py --update

then review the diff under tests/golden/ before committing.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the helper importable when this file is run standalone (`--update`), where pytest's
# conftest path bootstrap does not apply.
_TOOLS_ROOT = str(Path(__file__).resolve().parents[1] / "tools")
if _TOOLS_ROOT not in sys.path:
    sys.path.insert(0, _TOOLS_ROOT)

from cc5x_setcc_native import (  # noqa: E402  (path bootstrap must precede this import)
    default_pack_symbol_values,
    pack_config_symbols,
    render_config_block_from_symbols,
)
from cc5x_setcc_native_lib import picmeta as P  # noqa: E402
from cc5x_setcc_native_lib.headergen import (  # noqa: E402
    render_dynamic_config_section,
    render_full_header,
)

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
HEADER_GOLDEN = GOLDEN_DIR / "synth_pic16fsyn1.h"
CONFIG_BLOCK_GOLDEN = GOLDEN_DIR / "synth_pic16fsyn1.config5x.txt"


def build_synthetic_metadata() -> "P.DeviceMetadata":
    """A small, fixed enhanced-midrange (PIC14E) device.

    It deliberately includes a config setting whose state names start with a digit
    (``BBSIZE`` = ``512``/``8192``) to lock the security sanitization that turns a
    digit-leading state into a valid identifier in the *header* (``_512``) — the exact
    behavior that drifted once before. See ``test_digit_leading_state_divergence`` for the
    header-vs-managed-block divergence this also documents.
    """
    sfrs = [
        P.IniSfr(name="PORTA", address=0x00C, width=8),
        P.IniSfr(name="TRISA", address=0x08C, width=8),
    ]
    sfr_fields = [
        P.IniSfrField(name="RA0", address=0x00C, bit_position=0, width=1),
        P.IniSfrField(name="RA1", address=0x00C, bit_position=1, width=1),
    ]
    config_word = P.ConfigWord(
        address=0x8007,
        mask=0x3FFF,
        default=0x3FFF,
        name="CONFIG1",
        settings=[
            P.ConfigSetting(
                mask=0x0003,
                name="FOSC",
                description="Oscillator",
                values=[
                    P.ConfigValue(value=0x0, name="LP", description="LP oscillator"),
                    P.ConfigValue(value=0x3, name="INTOSC", description="internal oscillator"),
                ],
            ),
            P.ConfigSetting(
                mask=0x0030,
                name="BBSIZE",
                description="Boot Block Size",
                values=[
                    P.ConfigValue(value=0x30, name="512", description="Boot Block Size (Words) 512"),
                    P.ConfigValue(value=0x00, name="8192", description="Boot Block Size (Words) 8192"),
                ],
            ),
        ],
    )
    return P.DeviceMetadata(
        device="PIC16FSYN1",
        ini_arch="PIC14E",
        ini_procid="0x3000",
        rom_size_words=2048,
        banks=4,
        bank_size=128,
        sfr_count=len(sfrs),
        sfr_field_count=len(sfr_fields),
        config_word_count=1,
        config_setting_count=2,
        config_value_count=4,
        config_words=[config_word],
        sfrs=sfrs,
        sfr_fields=sfr_fields,
        ram_ranges=[P.MemoryRange(start=0x20, end=0x7F)],
        common_ranges=[P.MemoryRange(start=0x70, end=0x7F)],
        icd_ram_ranges=[],
        pic_summary=P.PicSummary(
            arch="PIC14E",
            procid="0x3000",
            dsid=None,
            has_program_space=True,
            has_data_space=True,
        ),
    )


def rendered_header() -> str:
    return render_full_header(build_synthetic_metadata())


def rendered_config_block() -> str:
    metadata = build_synthetic_metadata()
    symbols = pack_config_symbols(metadata)
    settings = default_pack_symbol_values(metadata)
    return render_config_block_from_symbols(
        source_label="synthetic PIC16FSYN1",
        family="5x",
        symbols=symbols,
        settings=settings,
    )


def _write_goldens() -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    HEADER_GOLDEN.write_text(rendered_header(), encoding="latin-1")
    CONFIG_BLOCK_GOLDEN.write_text(rendered_config_block(), encoding="latin-1")


class HeaderGoldenTests(unittest.TestCase):
    def test_full_header_matches_golden(self) -> None:
        expected = HEADER_GOLDEN.read_text(encoding="latin-1")
        self.assertEqual(
            rendered_header(),
            expected,
            "render_full_header drifted from the golden; if intentional, run "
            "`python tests/test_golden.py --update` and review tests/golden/.",
        )

    def test_config_block_matches_golden(self) -> None:
        expected = CONFIG_BLOCK_GOLDEN.read_text(encoding="latin-1")
        self.assertEqual(
            rendered_config_block(),
            expected,
            "render_config_block_from_symbols drifted from the golden; if intentional, run "
            "`python tests/test_golden.py --update` and review tests/golden/.",
        )

    def test_header_sanitizes_digit_leading_config_state(self) -> None:
        # Security hardening (commit 1a7db85) turns a digit-leading state into a valid
        # identifier in the header. Lock it so it cannot silently regress.
        header = rendered_header()
        self.assertIn("BBSIZE = _512", header)
        self.assertIn("BBSIZE = _8192", header)
        self.assertNotIn("BBSIZE = 512 ", header)

    def test_digit_leading_state_divergence(self) -> None:
        # KNOWN LATENT ISSUE (locked here, not endorsed): the header defines the state as
        # `_512` (sanitized) but the managed config block emits it raw as `512`. For a
        # generated-header project that syncs a digit-leading config value, the block would
        # reference a state the header never defines. This characterization test makes the
        # divergence visible so any future fix is a deliberate golden update.
        self.assertIn("BBSIZE = _512", rendered_header())
        self.assertIn("BBSIZE = 512", rendered_config_block())

    def test_dynamic_config_section_is_header_substring(self) -> None:
        # The standalone dynamic-config section must match what the full header embeds.
        self.assertIn(render_dynamic_config_section(build_synthetic_metadata()).rstrip("\n"),
                      rendered_header())


if __name__ == "__main__":
    if "--update" in sys.argv:
        _write_goldens()
        print(f"updated goldens in {GOLDEN_DIR}")
    else:
        unittest.main()
