from __future__ import annotations

import configparser
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from .packs import read_text_reference


@dataclass(frozen=True)
class IniSfr:
    name: str
    address: int
    width: int


@dataclass(frozen=True)
class IniSfrField:
    name: str
    address: int
    bit_position: int
    width: int


@dataclass(frozen=True)
class MemoryRange:
    start: int
    end: int


@dataclass(frozen=True)
class ConfigValue:
    value: int
    name: str
    description: str


@dataclass(frozen=True)
class ConfigSetting:
    mask: int
    name: str
    description: str
    values: list[ConfigValue] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigWord:
    address: int
    mask: int
    default: int
    name: str
    settings: list[ConfigSetting] = field(default_factory=list)


@dataclass(frozen=True)
class PicSummary:
    arch: str | None
    procid: str | None
    dsid: str | None
    has_program_space: bool
    has_data_space: bool


@dataclass(frozen=True)
class DeviceMetadata:
    device: str
    ini_arch: str | None
    ini_procid: str | None
    rom_size_words: int | None
    banks: int | None
    bank_size: int | None
    sfr_count: int
    sfr_field_count: int
    config_word_count: int
    config_setting_count: int
    config_value_count: int
    config_words: list[ConfigWord]
    sfrs: list[IniSfr]
    sfr_fields: list[IniSfrField]
    ram_ranges: list[MemoryRange]
    common_ranges: list[MemoryRange]
    icd_ram_ranges: list[MemoryRange]
    pic_summary: PicSummary | None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _parse_ini_range_list(value: str) -> list[MemoryRange]:
    ranges: list[MemoryRange] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        start_text, end_text = chunk.split("-", 1)
        ranges.append(MemoryRange(start=int(start_text, 16), end=int(end_text, 16)))
    return ranges


def parse_ini_text(
    text: str,
) -> tuple[dict[str, str], list[IniSfr], list[IniSfrField], dict[str, list[MemoryRange]]]:
    section_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        section_lines.append(stripped)
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string("\n".join(section_lines))
    section_name = parser.sections()[0]
    section = dict(parser.items(section_name))

    sfrs: list[IniSfr] = []
    sfr_fields: list[IniSfrField] = []
    range_groups = {
        "RAMBANK": [],
        "COMMON": [],
        "ICD1RAM": [],
        "ICD2RAM": [],
        "ICD3RAM": [],
    }
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("SFR="):
            _, payload = stripped.split("=", 1)
            name, address, width = payload.split(",", 2)
            sfrs.append(IniSfr(name=name, address=int(address, 16), width=int(width, 10)))
            continue
        if stripped.startswith("SFRFLD="):
            _, payload = stripped.split("=", 1)
            name, address, bit_position, width = payload.split(",", 3)
            sfr_fields.append(
                IniSfrField(
                    name=name,
                    address=int(address, 16),
                    bit_position=int(bit_position, 10),
                    width=int(width, 10),
                )
            )
            continue
        for key in range_groups:
            if stripped.startswith(f"{key}="):
                _, payload = stripped.split("=", 1)
                range_groups[key].extend(_parse_ini_range_list(payload))
                break
    return section, sfrs, sfr_fields, range_groups


def parse_cfgdata_text(text: str) -> list[ConfigWord]:
    words: list[ConfigWord] = []
    current_word: ConfigWord | None = None
    current_setting: ConfigSetting | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("CWORD:"):
            _, address, mask, default, name, *_rest = line.split(":")
            current_word = ConfigWord(
                address=int(address, 16),
                mask=int(mask, 16),
                default=int(default, 16),
                name=name.split(",")[0],
                settings=[],
            )
            words.append(current_word)
            current_setting = None
            continue
        if line.startswith("CSETTING:"):
            if current_word is None:
                continue
            _, mask, name, description = line.split(":", 3)
            setting = ConfigSetting(
                mask=int(mask, 16),
                name=name.split(",")[0],
                description=description,
                values=[],
            )
            current_word.settings.append(setting)
            current_setting = setting
            continue
        if line.startswith("CVALUE:"):
            if current_setting is None:
                continue
            _, value, name, description = line.split(":", 3)
            current_setting.values.append(
                ConfigValue(
                    value=int(value, 16),
                    name=name.split(",")[0],
                    description=description,
                )
            )
    return words


def parse_pic_xml_text(text: str) -> PicSummary:
    root = ET.fromstring(text)
    arch = root.attrib.get("{http://crownking/edc}arch")
    procid = root.attrib.get("{http://crownking/edc}procid")
    dsid = root.attrib.get("{http://crownking/edc}dsid")
    has_program_space = any(elem.tag.endswith("ProgramSpace") for elem in root.iter())
    has_data_space = any(elem.tag.endswith("DataSpace") for elem in root.iter())
    return PicSummary(
        arch=arch,
        procid=procid,
        dsid=dsid,
        has_program_space=has_program_space,
        has_data_space=has_data_space,
    )


def load_device_metadata(
    device: str,
    ini_reference: str | None,
    cfgdata_reference: str | None,
    pic_reference: str | None,
) -> DeviceMetadata:
    ini_section: dict[str, str] = {}
    sfrs: list[IniSfr] = []
    sfr_fields: list[IniSfrField] = []
    range_groups = {"RAMBANK": [], "COMMON": [], "ICD1RAM": [], "ICD2RAM": [], "ICD3RAM": []}
    config_words: list[ConfigWord] = []
    pic_summary: PicSummary | None = None

    if ini_reference:
        ini_text = read_text_reference(ini_reference, encoding="utf-8")
        ini_section, sfrs, sfr_fields, range_groups = parse_ini_text(ini_text)
    if cfgdata_reference:
        cfg_text = read_text_reference(cfgdata_reference, encoding="utf-8")
        config_words = parse_cfgdata_text(cfg_text)
    if pic_reference:
        pic_text = read_text_reference(pic_reference, encoding="utf-8")
        pic_summary = parse_pic_xml_text(pic_text)

    config_setting_count = sum(len(word.settings) for word in config_words)
    config_value_count = sum(len(setting.values) for word in config_words for setting in word.settings)

    return DeviceMetadata(
        device=device,
        ini_arch=ini_section.get("ARCH"),
        ini_procid=ini_section.get("PROCID"),
        rom_size_words=int(ini_section["ROMSIZE"], 16) if "ROMSIZE" in ini_section else None,
        banks=int(ini_section["BANKS"], 16) if "BANKS" in ini_section else None,
        bank_size=int(ini_section["BANKSIZE"], 16) if "BANKSIZE" in ini_section else None,
        sfr_count=len(sfrs),
        sfr_field_count=len(sfr_fields),
        config_word_count=len(config_words),
        config_setting_count=config_setting_count,
        config_value_count=config_value_count,
        config_words=config_words,
        sfrs=sfrs,
        sfr_fields=sfr_fields,
        ram_ranges=range_groups["RAMBANK"],
        common_ranges=range_groups["COMMON"],
        icd_ram_ranges=(
            range_groups["ICD1RAM"] + range_groups["ICD2RAM"] + range_groups["ICD3RAM"]
        ),
        pic_summary=pic_summary,
    )
