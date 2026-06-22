from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

try:
    from cc5x_setcc_native_lib.packs import (
        discover_pack_roots,
        find_device_in_atpacks,
        is_cc5x_device,
        list_devices_in_atpacks,
        list_devices_in_unpacked_packs,
        normalize_device_name,
        parse_pack_archive_info,
        parse_version,
    )
    from cc5x_setcc_native_lib.headergen import (
        render_dynamic_config_section,
        render_full_header,
    )
    from cc5x_setcc_native_lib.picmeta import (
        load_device_metadata,
        parse_cfgdata_text,
        parse_ini_text,
    )
except ModuleNotFoundError:
    from tools.cc5x_setcc_native_lib.packs import (
        discover_pack_roots,
        find_device_in_atpacks,
        is_cc5x_device,
        list_devices_in_atpacks,
        list_devices_in_unpacked_packs,
        normalize_device_name,
        parse_pack_archive_info,
        parse_version,
    )
    from tools.cc5x_setcc_native_lib.headergen import (
        render_dynamic_config_section,
        render_full_header,
    )
    from tools.cc5x_setcc_native_lib.picmeta import (
        load_device_metadata,
        parse_cfgdata_text,
        parse_ini_text,
    )


def create_atpack(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


class PackReaderTests(unittest.TestCase):
    def test_normalize_pic_device_name(self) -> None:
        self.assertEqual(normalize_device_name("16f1509"), "PIC16F1509")

    def test_parse_pack_archive_info(self) -> None:
        info = parse_pack_archive_info(Path("Microchip.PIC16F1xxxx_DFP.1.29.444.atpack"))
        self.assertEqual(info.family, "PIC16F1xxxx_DFP")
        self.assertEqual(info.version, "1.29.444")
        self.assertEqual(info.version_key, (1, 29, 444))

    def test_is_cc5x_device_filters_family_scope(self) -> None:
        self.assertTrue(is_cc5x_device("PIC10F322"))
        self.assertTrue(is_cc5x_device("12f1840"))
        self.assertTrue(is_cc5x_device("pic16f19195"))
        self.assertFalse(is_cc5x_device("PIC18F47Q10"))

    def test_find_device_in_atpacks_prefers_newest_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            older = root / "Microchip.PIC12-16F1xxx_DFP.1.8.254.atpack"
            newer = root / "Microchip.PIC16F1xxxx_DFP.1.29.444.atpack"
            create_atpack(
                older,
                {
                    "edc/PIC16F15313.PIC": "old pic",
                    "xc8/pic/dat/ini/16f15313.ini": "old ini",
                    "xc8/pic/dat/cfgdata/16f15313.cfgdata": "old cfg",
                    "Microchip.PIC12-16F1xxx_DFP.pdsc": "old pdsc",
                },
            )
            create_atpack(
                newer,
                {
                    "edc/PIC16F15313.PIC": "new pic",
                    "xc8/pic/dat/ini/16f15313.ini": "new ini",
                    "xc8/pic/dat/cfgdata/16f15313.cfgdata": "new cfg",
                    "Microchip.PIC16F1xxxx_DFP.pdsc": "new pdsc",
                },
            )

            result = find_device_in_atpacks("PIC16F15313", [root])

            self.assertEqual(result["pack_family"], "PIC16F1xxxx_DFP")
            self.assertEqual(result["pack_version"], "1.29.444")
            self.assertIn("PIC16F15313.PIC", result["pic"] or "")
            self.assertIn("16f15313.ini", result["ini"] or "")
            self.assertIn("16f15313.cfgdata", result["cfgdata"] or "")

    def test_find_device_in_atpacks_supports_accessory_pic_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "Microchip.PIC12-16F1xxx_DFP.1.8.254.atpack"
            create_atpack(
                archive,
                {
                    "edc/AC244051_AS_PIC16F1509.PIC": "pic",
                    "xc8/pic/dat/ini/16f1509.ini": "ini",
                    "xc8/pic/dat/cfgdata/16f1509.cfgdata": "cfg",
                    "Microchip.PIC12-16F1xxx_DFP.pdsc": "pdsc",
                },
            )

            result = find_device_in_atpacks("16f1509", [root])

            self.assertEqual(result["pack_family"], "PIC12-16F1xxx_DFP")
            self.assertIn("AC244051_AS_PIC16F1509.PIC", result["pic"] or "")

    def test_list_devices_in_atpacks_prefers_newest_version_and_filters_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            older = root / "Microchip.PIC10-12Fxxx_DFP.1.8.100.atpack"
            newer = root / "Microchip.PIC10-12Fxxx_DFP.1.8.184.atpack"
            create_atpack(
                older,
                {
                    "edc/PIC10F320.PIC": "old 10f",
                    "edc/PIC18F47Q10.PIC": "ignored 18f",
                    "Microchip.PIC10-12Fxxx_DFP.pdsc": "pdsc old",
                },
            )
            create_atpack(
                newer,
                {
                    "edc/PIC10F320.PIC": "new 10f",
                    "edc/AC244051_AS_PIC12F1840.PIC": "12f",
                    "Microchip.PIC10-12Fxxx_DFP.pdsc": "pdsc new",
                },
            )

            devices = list_devices_in_atpacks([root])

            self.assertEqual([item["device"] for item in devices], ["PIC10F320", "PIC12F1840"])
            self.assertTrue(all(item["pack_version"] == "1.8.184" for item in devices))

            tenf_only = list_devices_in_atpacks([root], prefixes=("PIC10F",))
            self.assertEqual([item["device"] for item in tenf_only], ["PIC10F320"])

    def test_parse_ini_and_cfgdata(self) -> None:
        ini_text = """
[16F1509]
ARCH=PIC14E
PROCID=1509
ROMSIZE=2000
BANKS=20
SFR=PORTA,C,8
SFR=PORTB,D,8
"""
        section, sfrs, sfr_fields, range_groups = parse_ini_text(ini_text)
        self.assertEqual(section["ARCH"], "PIC14E")
        self.assertEqual(section["PROCID"], "1509")
        self.assertEqual(len(sfrs), 2)
        self.assertEqual(len(sfr_fields), 0)
        self.assertEqual(len(range_groups["RAMBANK"]), 0)
        self.assertEqual(sfrs[0].name, "PORTA")
        self.assertEqual(sfrs[0].address, 0xC)

        cfg_text = """
CWORD:8007:3EFF:3FFF:CONFIG1
CSETTING:7:FOSC:Oscillator Selection Bits
CVALUE:4:INTOSC:INTOSC oscillator
CVALUE:0:LP:LP oscillator
"""
        words = parse_cfgdata_text(cfg_text)
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0].name, "CONFIG1")
        self.assertEqual(words[0].settings[0].name, "FOSC")
        self.assertEqual(words[0].settings[0].values[0].name, "INTOSC")

    def test_load_device_metadata_from_archive_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "Microchip.PIC12-16F1xxx_DFP.1.8.254.atpack"
            create_atpack(
                archive,
                {
                    "edc/PIC16F1509.PIC": "<?xml version='1.0'?><edc:PIC xmlns:edc='http://crownking/edc' edc:arch='16Exxx' edc:procid='1509' edc:dsid='abc'><ProgramSpace/><DataSpace/></edc:PIC>",
                    "xc8/pic/dat/ini/16f1509.ini": "[16F1509]\nARCH=PIC14E\nPROCID=1509\nROMSIZE=2000\nBANKS=20\nSFR=PORTA,C,8\n",
                    "xc8/pic/dat/cfgdata/16f1509.cfgdata": "CWORD:8007:3EFF:3FFF:CONFIG1\nCSETTING:7:FOSC:Oscillator Selection Bits\nCVALUE:4:INTOSC:INTOSC oscillator\n",
                    "Microchip.PIC12-16F1xxx_DFP.pdsc": "pdsc",
                },
            )
            result = find_device_in_atpacks("PIC16F1509", [root])
            metadata = load_device_metadata(
                device="PIC16F1509",
                ini_reference=result["ini"],
                cfgdata_reference=result["cfgdata"],
                pic_reference=result["pic"],
            )
            self.assertEqual(metadata.ini_arch, "PIC14E")
            self.assertEqual(metadata.rom_size_words, 0x2000)
            self.assertEqual(metadata.sfr_count, 1)
            self.assertEqual(metadata.sfr_field_count, 0)
            self.assertEqual(metadata.config_word_count, 1)
            self.assertEqual(metadata.pic_summary.arch, "16Exxx")

    def test_render_dynamic_config_section(self) -> None:
        cfg_text = (
            "CWORD:8007:3EFF:3FFF:CONFIG1\n"
            "CSETTING:7:FOSC:Oscillator Selection Bits\n"
            "CVALUE:4:INTOSC:INTOSC oscillator\n"
            "CVALUE:0:LP:LP oscillator\n"
        )
        words = parse_cfgdata_text(cfg_text)
        metadata = load_device_metadata(
            device="PIC16F1509",
            ini_reference=None,
            cfgdata_reference=None,
            pic_reference=None,
        )
        metadata = metadata.__class__(
            device=metadata.device,
            ini_arch=metadata.ini_arch,
            ini_procid=metadata.ini_procid,
            rom_size_words=metadata.rom_size_words,
            banks=metadata.banks,
            bank_size=metadata.bank_size,
            sfr_count=metadata.sfr_count,
            sfr_field_count=metadata.sfr_field_count,
            config_word_count=len(words),
            config_setting_count=1,
            config_value_count=2,
            config_words=words,
            sfrs=metadata.sfrs,
            sfr_fields=metadata.sfr_fields,
            ram_ranges=metadata.ram_ranges,
            common_ranges=metadata.common_ranges,
            icd_ram_ranges=metadata.icd_ram_ranges,
            pic_summary=metadata.pic_summary,
        )
        rendered = render_dynamic_config_section(metadata)
        self.assertIn("#if __CC5X__ >= 3600  &&  !defined _DISABLE_DYN_CONFIG", rendered)
        self.assertIn("#pragma config /1 0x3FF8 FOSC = LP // LP oscillator", rendered)
        self.assertIn("#pragma config /1 0x3FFC FOSC = INTOSC // INTOSC oscillator", rendered)
        self.assertTrue(rendered.rstrip().endswith("#endif"))

    def test_render_full_header(self) -> None:
        ini_text = """
[16F1509]
ARCH=PIC14E
PROCID=1509
ROMSIZE=2000
BANKS=20
BANKSIZE=80
RAMBANK=20-7F
COMMON=70-7F
SFR=INDF0,0,8
SFR=INTCON,B,8
SFR=PORTA,C,8
SFR=PORTB,D,8
SFR=RC1REG,119,8
SFR=RCREG,119,8
SFRFLD=IOCIF,B,0,1
SFRFLD=RA0,C,0,1
SFRFLD=RA1,C,1,1
"""
        cfg_text = """
CWORD:8007:3EFF:3FFF:CONFIG1
CSETTING:7:FOSC:Oscillator Selection Bits
CVALUE:4:INTOSC:INTOSC oscillator
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "Microchip.PIC12-16F1xxx_DFP.1.8.254.atpack"
            create_atpack(
                archive,
                {
                    "edc/PIC16F1509.PIC": "<?xml version='1.0'?><edc:PIC xmlns:edc='http://crownking/edc' edc:arch='16Exxx' edc:procid='1509' edc:dsid='abc'><ProgramSpace/><DataSpace/></edc:PIC>",
                    "xc8/pic/dat/ini/16f1509.ini": ini_text.strip() + "\n",
                    "xc8/pic/dat/cfgdata/16f1509.cfgdata": cfg_text.strip() + "\n",
                    "Microchip.PIC12-16F1xxx_DFP.pdsc": "pdsc",
                },
            )
            result = find_device_in_atpacks("PIC16F1509", [root])
            metadata = load_device_metadata(
                device="PIC16F1509",
                ini_reference=result["ini"],
                cfgdata_reference=result["cfgdata"],
                pic_reference=result["pic"],
            )
        rendered = render_full_header(metadata)
        self.assertIn("#pragma chip PIC16F1509, core 14 enh, code 8192, ram 32 : 0x7F // 96 bytes", rendered)
        self.assertIn("char PORTA @ 0xC;", rendered)
        self.assertIn("char RCREG @ RC1REG;", rendered)
        self.assertIn("bit IOCIF @ INTCON.0;", rendered)
        self.assertIn("bit RA0 @ PORTA.0;", rendered)
        self.assertIn("#pragma config /1 0x3FFC FOSC = INTOSC // INTOSC oscillator", rendered)

    def test_render_full_header_alias_bits_and_suppression(self) -> None:
        ini_text = """
[16F15313]
ARCH=PIC14EX
PROCID=A2AE
ROMSIZE=800
BANKS=40
BANKSIZE=80
RAMBANK=20-7F
COMMON=70-7F
SFR=INDF0,0,8
SFR=INTCON,B,8
SFR=ADRESL,9B,8
SFR=ADCON0,9D,8
SFR=RC1STA,11D,8
SFR=RCSTA1,11D,8
SFR=TX1STA,11E,8
SFR=TXSTA1,11E,8
SFR=BAUD1CON,11F,8
SFR=BAUDCON1,11F,8
SFRFLD=INTEDG,B,0,1
SFRFLD=ADRESL0,9B,0,1
SFRFLD=ADRESL1,9B,1,1
SFRFLD=ADRESL2,9B,2,1
SFRFLD=ADRESL3,9B,3,1
SFRFLD=ADRESL4,9B,4,1
SFRFLD=ADRESL5,9B,5,1
SFRFLD=ADRESL6,9B,6,1
SFRFLD=ADRESL7,9B,7,1
SFRFLD=ADON,9D,0,1
SFRFLD=GOnDONE,9D,1,1
SFRFLD=RX9,11D,6,1
SFRFLD=SYNC,11E,4,1
SFRFLD=BRG16,11F,3,1
"""
        cfg_text = """
CWORD:8007:3EFF:3FFF:CONFIG1
CSETTING:7:FEXTOSC:Oscillator Selection Bits
CVALUE:4:OFF:Oscillator not enabled
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "Microchip.PIC16F1xxxx_DFP.1.29.444.atpack"
            create_atpack(
                archive,
                {
                    "edc/PIC16F15313.PIC": "<?xml version='1.0'?><edc:PIC xmlns:edc='http://crownking/edc' edc:arch='16Exxx' edc:procid='A2AE' edc:dsid='abc'><ProgramSpace/><DataSpace/></edc:PIC>",
                    "xc8/pic/dat/ini/16f15313.ini": ini_text.strip() + "\n",
                    "xc8/pic/dat/cfgdata/16f15313.cfgdata": cfg_text.strip() + "\n",
                    "Microchip.PIC16F1xxxx_DFP.pdsc": "pdsc",
                },
            )
            result = find_device_in_atpacks("PIC16F15313", [root])
            metadata = load_device_metadata(
                device="PIC16F15313",
                ini_reference=result["ini"],
                cfgdata_reference=result["cfgdata"],
                pic_reference=result["pic"],
            )
        rendered = render_full_header(metadata)
        self.assertIn("bit ADGO @ ADCON0.1;", rendered)
        self.assertIn("bit GO @ ADCON0.1;", rendered)
        self.assertNotIn("bit GOnDONE @ ADCON0.1;", rendered)
        self.assertNotIn("bit ADRESL0 @ ADRESL.0;", rendered)
        self.assertIn("bit RX9_1 @ RCSTA1.6;", rendered)
        self.assertIn("bit TXSYNC1 @ TXSTA1.4;", rendered)
        self.assertIn("bit BRG16_1 @ BAUDCON1.3;", rendered)


class UnpackedPackDiscoveryTests(unittest.TestCase):
    def _make_pack(self, root: Path, family: str, version: str, devices: list[str]) -> None:
        edc = root / family / version / "edc"
        edc.mkdir(parents=True)
        for device in devices:
            (edc / f"{device}.PIC").write_text("<edc/>", encoding="latin-1")

    def test_lists_cc5x_devices_from_unpacked_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._make_pack(root, "PIC12-16F1xxx_DFP", "1.8.254", ["PIC16F1509", "PIC12F1840"])
            result = list_devices_in_unpacked_packs([root])
        names = [item["device"] for item in result]
        self.assertEqual(names, ["PIC12F1840", "PIC16F1509"])
        self.assertEqual(result[1]["pack_version"], "1.8.254")

    def test_filters_non_cc5x_devices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # PIC18F is outside the CC5X family scope and must be excluded.
            self._make_pack(root, "PIC18F_DFP", "1.0.0", ["PIC18F4580"])
            self._make_pack(root, "PIC16F1xxxx_DFP", "1.0.0", ["PIC16F15313"])
            result = list_devices_in_unpacked_packs([root])
        self.assertEqual([item["device"] for item in result], ["PIC16F15313"])

    def test_keeps_highest_version_per_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._make_pack(root, "PIC12-16F1xxx_DFP", "1.8.254", ["PIC16F1509"])
            self._make_pack(root, "PIC12-16F1xxx_DFP", "1.10.1", ["PIC16F1509"])
            result = list_devices_in_unpacked_packs([root])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pack_version"], "1.10.1")

    def test_discover_pack_roots_env_override_is_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override = Path(tmpdir) / "custom"
            with unittest.mock.patch.dict(
                os.environ, {"CC5X_PACK_ROOTS": str(override)}, clear=False
            ):
                roots = discover_pack_roots()
        self.assertEqual(roots[0], override)

    def test_discover_pack_roots_orders_mplabx_versions_numerically(self) -> None:
        # Audit A6: v6.30 must sort newer than v6.5 (numeric, not lexical).
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            for version in ("v6.5", "v6.30"):
                (base / version / "packs" / "Microchip").mkdir(parents=True)
            with unittest.mock.patch(
                "cc5x_setcc_native_lib.packs.mplabx_install_bases", return_value=[base]
            ), unittest.mock.patch.dict(os.environ, {}, clear=True):
                roots = discover_pack_roots()
        v630 = base / "v6.30" / "packs" / "Microchip"
        v65 = base / "v6.5" / "packs" / "Microchip"
        self.assertLess(roots.index(v630), roots.index(v65))


class ParseVersionTests(unittest.TestCase):
    """Audit A9: one shared version parser with consistent non-numeric handling."""

    def test_pack_version(self) -> None:
        self.assertEqual(parse_version("1.29.444"), (1, 29, 444))

    def test_strips_leading_v(self) -> None:
        self.assertEqual(parse_version("v6.30"), (6, 30))
        self.assertGreater(parse_version("v6.30"), parse_version("v6.5"))

    def test_non_numeric_and_empty_yield_empty_tuple(self) -> None:
        self.assertEqual(parse_version("1.x.3"), ())
        self.assertEqual(parse_version(""), ())
        self.assertEqual(parse_version(None), ())


if __name__ == "__main__":
    unittest.main()
