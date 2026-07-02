"""Audit #6: malformed pack metadata must not steer generated `#pragma` directives.

These lock the hardening of `headergen` against untrusted pack metadata: an unsupported
architecture is rejected (not emitted as a bogus `core`), a config value is masked to its
setting's bits before it joins the config word, and a malformed memory range cannot produce
a negative byte count in the chip pragma.
"""

from __future__ import annotations

import dataclasses
import unittest

from cc5x_setcc_native_lib import picmeta as P
from cc5x_setcc_native_lib.headergen import KNOWN_ARCHS, render_full_header

from test_golden import build_synthetic_metadata


class UnknownArchitectureTests(unittest.TestCase):
    def test_unknown_arch_is_rejected(self) -> None:
        # PIC12E/PIC12IE baseline parts (e.g. PIC16F527) match the CC5X device prefixes but
        # have no mapped core; previously they emitted `core pic12e` silently.
        metadata = dataclasses.replace(build_synthetic_metadata(), ini_arch="PIC12E")
        with self.assertRaises(ValueError) as ctx:
            render_full_header(metadata)
        self.assertIn("PIC12E", str(ctx.exception))

    def test_empty_arch_is_rejected(self) -> None:
        for bad in (None, "", "   "):
            metadata = dataclasses.replace(build_synthetic_metadata(), ini_arch=bad)
            with self.assertRaises(ValueError):
                render_full_header(metadata)

    def test_known_archs_all_render(self) -> None:
        # Every mapped architecture still produces a header with a `#pragma chip ... core`.
        for arch in KNOWN_ARCHS:
            metadata = dataclasses.replace(build_synthetic_metadata(), ini_arch=arch)
            header = render_full_header(metadata)
            self.assertIn("#pragma chip", header)
            self.assertNotIn("core unknown", header)


class ConfigValueMaskingTests(unittest.TestCase):
    def _device_with_word(self, word: P.ConfigWord) -> P.DeviceMetadata:
        return dataclasses.replace(
            build_synthetic_metadata(),
            config_words=[word],
            config_word_count=1,
            config_setting_count=len(word.settings),
            config_value_count=sum(len(s.values) for s in word.settings),
        )

    def test_value_bits_outside_setting_mask_are_masked_off(self) -> None:
        # default 0x0000 makes the masking observable: a poisoned FOSC value (mask 0x0003)
        # carries a stray 0x0040 bit. Masked, the word is 0x3; unmasked it would be 0x43.
        word = P.ConfigWord(
            address=0x8007,
            mask=0x00FF,
            default=0x0000,
            name="CONFIG1",
            settings=[
                P.ConfigSetting(
                    mask=0x0003,
                    name="FOSC",
                    description="Oscillator",
                    values=[P.ConfigValue(value=0x0043, name="LP", description="LP")],
                )
            ],
        )
        header = self._render(self._device_with_word(word))
        self.assertIn("#pragma config /1 0x3 FOSC = LP", header)
        self.assertNotIn("0x43", header)

    def test_stray_bit_does_not_corrupt_neighbouring_setting(self) -> None:
        # Two settings in one word. A poisoned FOSC value with a bit inside BBSIZE's mask
        # (0x0030) must not flip BBSIZE: masking confines it to FOSC's own 0x0003 bits.
        word = P.ConfigWord(
            address=0x8007,
            mask=0x00FF,
            default=0x0000,
            name="CONFIG1",
            settings=[
                P.ConfigSetting(
                    mask=0x0003,
                    name="FOSC",
                    description="Oscillator",
                    values=[P.ConfigValue(value=0x0031, name="LP", description="LP")],
                ),
                P.ConfigSetting(
                    mask=0x0030,
                    name="BBSIZE",
                    description="Boot Block",
                    values=[P.ConfigValue(value=0x0000, name="MAX", description="max")],
                ),
            ],
        )
        header = self._render(self._device_with_word(word))
        # FOSC: 0x0031 & 0x0003 = 0x0001 (the 0x0030 BBSIZE bit is dropped).
        self.assertIn("#pragma config /1 0x1 FOSC = LP", header)

    @staticmethod
    def _render(metadata: P.DeviceMetadata) -> str:
        return render_full_header(metadata)


class MemoryRangeTests(unittest.TestCase):
    def test_malformed_range_does_not_emit_negative_bytes(self) -> None:
        base = build_synthetic_metadata()
        # end < start: a corrupt RAMBANK record. The byte comment must not go negative.
        metadata = dataclasses.replace(base, ram_ranges=[P.MemoryRange(start=0x80, end=0x20)])
        header = render_full_header(metadata)
        self.assertNotIn("-", header.split("#pragma chip", 1)[1].splitlines()[0])


class SanitizedIdentifierCollisionTests(unittest.TestCase):
    """Two distinct raw pack names that sanitize to the same C identifier must not both be
    emitted, or the header fails to compile on a duplicate symbol (audit: identifiers collide
    after sanitization)."""

    def test_colliding_bit_names_emit_one_declaration(self) -> None:
        base = build_synthetic_metadata()
        # "RA.0" and "RA-0" both sanitize to "RA_0"; only one bit declaration may survive.
        fields = [
            P.IniSfrField(name="RA.0", address=0x00C, bit_position=0, width=1),
            P.IniSfrField(name="RA-0", address=0x00C, bit_position=1, width=1),
        ]
        metadata = dataclasses.replace(base, sfr_fields=fields, sfr_field_count=len(fields))
        header = render_full_header(metadata)
        decls = [line for line in header.splitlines() if "RA_0" in line]
        self.assertEqual(len(decls), 1, decls)

    def test_bit_colliding_with_register_char_is_dropped(self) -> None:
        # Cross-section collision: an 8-bit SFR "GP_IO" emits `char GP_IO`; a 1-bit field
        # "GP-IO" sanitizes to the same identifier and must NOT also emit `bit GP_IO`, or the
        # header declares GP_IO twice across the char/bit namespaces (audit: cross-section).
        base = build_synthetic_metadata()
        sfrs = [P.IniSfr(name="GP_IO", address=0x00C, width=8)]
        fields = [P.IniSfrField(name="GP-IO", address=0x00C, bit_position=0, width=1)]
        metadata = dataclasses.replace(
            base,
            sfrs=sfrs,
            sfr_count=len(sfrs),
            sfr_fields=fields,
            sfr_field_count=len(fields),
        )
        header = render_full_header(metadata)
        decls = [line for line in header.splitlines() if "GP_IO" in line]
        self.assertEqual(len(decls), 1, decls)

    def test_colliding_sfr_names_emit_one_declaration(self) -> None:
        base = build_synthetic_metadata()
        # Two registers at different addresses whose names sanitize alike ("GP IO"/"GP-IO" ->
        # "GP_IO") must not both declare the same identifier.
        sfrs = [
            P.IniSfr(name="GP IO", address=0x00C, width=8),
            P.IniSfr(name="GP-IO", address=0x00D, width=8),
        ]
        metadata = dataclasses.replace(
            base, sfrs=sfrs, sfr_count=len(sfrs), sfr_fields=[], sfr_field_count=0
        )
        header = render_full_header(metadata)
        decls = [line for line in header.splitlines() if "GP_IO" in line]
        self.assertEqual(len(decls), 1, decls)


class BitNameFormatTests(unittest.TestCase):
    def _metadata(self) -> P.DeviceMetadata:
        base = build_synthetic_metadata()
        sfrs = [*base.sfrs, P.IniSfr(name="ADCON0", address=0x09D, width=8)]
        fields = [
            *base.sfr_fields,
            P.IniSfrField(name="ON", address=0x09D, bit_position=0, width=1),
            P.IniSfrField(name="GO_nDONE", address=0x09D, bit_position=1, width=1),
        ]
        return dataclasses.replace(
            base, sfrs=sfrs, sfr_count=len(sfrs), sfr_fields=fields, sfr_field_count=len(fields)
        )

    def test_combined_is_default_and_preserves_short_names(self) -> None:
        header = render_full_header(self._metadata())
        self.assertIn("bit ON @ ADCON0.0;", header)
        self.assertIn("bit GO_nDONE @ ADCON0.1;", header)

    def test_long_format_uses_setcc_register_bit_names(self) -> None:
        header = render_full_header(self._metadata(), bit_name_format="long")
        self.assertIn("bit ADCON0_ON @ ADCON0.0;", header)
        self.assertIn("bit ADCON0_GO_nDONE @ ADCON0.1;", header)
        self.assertNotIn("bit ON @ ADCON0.0;", header)

    def test_invalid_bit_name_format_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_full_header(self._metadata(), bit_name_format="bogus")


if __name__ == "__main__":
    unittest.main()
