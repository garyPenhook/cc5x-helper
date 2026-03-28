from __future__ import annotations

from dataclasses import dataclass

from .picmeta import DeviceMetadata, IniSfr, IniSfrField, MemoryRange


ENHANCED_PREDEFINED_REGISTERS = {
    "INDF0",
    "INDF1",
    "FSR0",
    "FSR0L",
    "FSR0H",
    "FSR1",
    "FSR1L",
    "FSR1H",
    "WREG",
    "PCL",
    "PCLATH",
    "BSR",
    "STATUS",
    "INTCON",
}

PREDEFINED_FIELD_ADDRESSES = {0x03}
SPECIAL_BIT_NAME_ALIASES = {
    "GOnDONE": ["ADGO", "GO"],
}
SPECIAL_ALIAS_BIT_BASE_NAMES = {
    ("TXSTA1", "SYNC"): "TXSYNC",
}
PIC14EX_ALIAS_WHITELIST = {
    "RCREG1",
    "TXREG1",
    "RCSTA1",
    "TXSTA1",
    "BAUDCON1",
    "TMR1GATE",
    "TMR1CLK",
    "TMR2",
    "PR2",
    "CMSTAT",
}


@dataclass(frozen=True)
class HeaderProfile:
    core: str
    define_int_style: bool
    wide_const_guard: str | None
    wide_const_value: str | None


def _render_config_line(word_index: int, word, setting, value) -> str:
    resolved_word = (word.default & ~setting.mask) | value.value
    return (
        f"#pragma config /{word_index} 0x{resolved_word:0X} "
        f"{setting.name} = {value.name} // {value.description}"
    )


def render_dynamic_config_section(metadata: DeviceMetadata) -> str:
    lines = ["#if __CC5X__ >= 3600  &&  !defined _DISABLE_DYN_CONFIG"]
    for word_index, word in enumerate(metadata.config_words, start=1):
        for setting in word.settings:
            for value in sorted(setting.values, key=lambda item: item.value):
                lines.append(_render_config_line(word_index, word, setting, value))
    lines.append("#endif")
    return "\n".join(lines) + "\n"


def _profile_for_arch(arch: str | None) -> HeaderProfile:
    arch = (arch or "").upper()
    if arch == "PIC14EX":
        return HeaderProfile(
            core="14 enh2",
            define_int_style=True,
            wide_const_guard="#if __CC5X__ >= 3505",
            wide_const_value="h",
        )
    if arch == "PIC14E":
        return HeaderProfile(
            core="14 enh",
            define_int_style=True,
            wide_const_guard=None,
            wide_const_value="p",
        )
    if arch == "PIC14":
        return HeaderProfile(
            core="14",
            define_int_style=False,
            wide_const_guard=None,
            wide_const_value=None,
        )
    if arch == "PIC12":
        return HeaderProfile(
            core="12",
            define_int_style=False,
            wide_const_guard=None,
            wide_const_value=None,
        )
    if arch == "PIC16":
        return HeaderProfile(
            core="16",
            define_int_style=False,
            wide_const_guard=None,
            wide_const_value=None,
        )
    return HeaderProfile(
        core=arch.lower() if arch else "unknown",
        define_int_style=False,
        wide_const_guard=None,
        wide_const_value=None,
    )


def _sum_range_bytes(ranges: list[MemoryRange]) -> int:
    return sum(item.end - item.start + 1 for item in ranges)


def _ram_limit(metadata: DeviceMetadata) -> int | None:
    base_ranges = metadata.ram_ranges + metadata.icd_ram_ranges
    if not base_ranges:
        return None
    current_max = max(item.end for item in base_ranges)
    changed = True
    while changed:
        changed = False
        for item in metadata.common_ranges:
            if item.start <= current_max + 1 and item.end > current_max:
                current_max = item.end
                changed = True
    return current_max


def _render_chip_pragma(metadata: DeviceMetadata) -> list[str]:
    profile = _profile_for_arch(metadata.ini_arch)
    code_size = metadata.rom_size_words or 0
    ram_bytes = _sum_range_bytes(metadata.ram_ranges)
    ram_origin = metadata.ram_ranges[0].start if metadata.ram_ranges else 0
    ram_limit = _ram_limit(metadata)
    line = (
        f"#pragma chip {metadata.device}, core {profile.core}, code {code_size}, ram {ram_origin}"
    )
    if ram_limit is not None:
        line += f" : 0x{ram_limit:X}"
    if ram_bytes:
        line += f" // {ram_bytes} bytes"
    lines = [line]
    if metadata.common_ranges:
        first_common = metadata.common_ranges[0]
        lines.append(
            f"#pragma ramdef  0x{first_common.start:X} : 0x{first_common.end:X} mapped_into_all_banks"
        )
    return lines


def _render_header_prelude(metadata: DeviceMetadata) -> list[str]:
    profile = _profile_for_arch(metadata.ini_arch)
    lines = ["// HEADER FILE"]
    lines.extend(_render_chip_pragma(metadata))
    lines.append("")
    if profile.define_int_style:
        lines.append("#define INT_enh_style")
        lines.append("")
    if profile.wide_const_value:
        if profile.wide_const_guard:
            lines.append(profile.wide_const_guard)
            lines.append(f" #pragma wideConstData {profile.wide_const_value}")
            lines.append("#endif")
        else:
            lines.append(f"#pragma wideConstData {profile.wide_const_value}")
        lines.append("")
    lines.extend(_predefined_comment_lines(metadata))
    return lines


def _predefined_comment_lines(metadata: DeviceMetadata) -> list[str]:
    arch = (metadata.ini_arch or "").upper()
    if arch == "PIC12":
        return [
            "/* Predefined:",
            "  char W;",
            "  char INDF, TMR0, PCL, STATUS, FSR;",
            "  char OPTION;",
            "  bit Carry, DC, Zero_, PD, TO;",
            "*/",
            "",
        ]
    if arch == "PIC14":
        return [
            "/* Predefined:",
            "  char W;",
            "  char INDF, TMR0, PCL, STATUS, FSR, PORTA;",
            "  char OPTION;",
            "  char PCLATH, INTCON;",
            "  bit Carry, DC, Zero_, PD, TO, RP0, RP1, IRP;",
            "  bit RBIF, INTF, T0IF, RBIE, INTE, T0IE, GIE;",
            "  bit PA0, PA1;  // PCLATH",
            "*/",
            "",
        ]
    return [
        "/* Predefined:",
        "  char *FSR0, *FSR1;",
        "  char INDF0, INDF1;",
        "  char FSR0L, FSR0H, FSR1L, FSR1H;",
        "  char W, WREG;",
        "  char PCL, PCLATH, BSR, STATUS, INTCON;",
        "  bit Carry, DC, Zero_, PD, TO;",
        "*/",
        "",
    ]


def _predefined_registers(metadata: DeviceMetadata) -> set[str]:
    arch = (metadata.ini_arch or "").upper()
    if arch == "PIC12":
        return {"INDF", "TMR0", "PCL", "STATUS", "FSR", "OPTION"}
    if arch == "PIC14":
        return {"INDF", "TMR0", "PCL", "STATUS", "FSR", "PORTA", "OPTION", "PCLATH", "INTCON"}
    return set(ENHANCED_PREDEFINED_REGISTERS)


def _predefined_bit_names(metadata: DeviceMetadata) -> set[str]:
    arch = (metadata.ini_arch or "").upper()
    if arch == "PIC12":
        return {"Carry", "DC", "Zero_", "PD", "TO"}
    if arch == "PIC14":
        return {
            "Carry",
            "DC",
            "Zero_",
            "PD",
            "TO",
            "RP0",
            "RP1",
            "IRP",
            "RBIF",
            "INTF",
            "T0IF",
            "RBIE",
            "INTE",
            "T0IE",
            "GIE",
            "PA0",
            "PA1",
        }
    return {"Carry", "DC", "Zero_", "PD", "TO"}


def _canonical_8bit_sfrs(metadata: DeviceMetadata) -> tuple[dict[int, IniSfr], list[IniSfr]]:
    predefined_registers = _predefined_registers(metadata)
    canonical_by_address: dict[int, IniSfr] = {}
    ordered: list[IniSfr] = []
    for sfr in metadata.sfrs:
        if sfr.width != 8:
            continue
        if sfr.name in predefined_registers:
            continue
        if sfr.address not in canonical_by_address:
            canonical_by_address[sfr.address] = sfr
            ordered.append(sfr)
    return canonical_by_address, ordered


def _render_sfr_section(metadata: DeviceMetadata) -> list[str]:
    canonical_by_address, ordered = _canonical_8bit_sfrs(metadata)
    predefined_registers = _predefined_registers(metadata)
    use_pragma_char = (metadata.ini_arch or "").upper() == "PIC14"
    lines: list[str] = []
    for sfr in ordered:
        if use_pragma_char:
            lines.append(f"#pragma char {sfr.name} @ 0x{sfr.address:X}")
        else:
            lines.append(f"char {sfr.name} @ 0x{sfr.address:X};")
        for alias in metadata.sfrs:
            if alias.address != sfr.address or alias.width != 8 or alias.name == sfr.name:
                continue
            if alias.name in predefined_registers:
                continue
            if not _should_emit_alias(metadata, sfr.name, alias.name):
                continue
            if use_pragma_char:
                lines.append(f"#pragma char {alias.name} @ {sfr.name}")
            else:
                lines.append(f"char {alias.name} @ {sfr.name};")
        lines.append("")
    if lines and not lines[-1]:
        lines.pop()
    return lines


def _normalize_bit_name(name: str) -> str:
    if name.startswith("n") and len(name) > 1 and name[1].isalnum():
        return f"{name[1:]}_"
    return name


def _build_register_groups(
    metadata: DeviceMetadata,
) -> tuple[dict[int, IniSfr], dict[int, list[IniSfr]]]:
    canonical_by_address, ordered = _canonical_8bit_sfrs(metadata)
    predefined_registers = _predefined_registers(metadata)
    alias_groups: dict[int, list[IniSfr]] = {sfr.address: [] for sfr in ordered}
    for alias in metadata.sfrs:
        if alias.width != 8 or alias.name in predefined_registers:
            continue
        canonical = canonical_by_address.get(alias.address)
        if canonical is None or canonical.name == alias.name:
            continue
        if not _should_emit_alias(metadata, canonical.name, alias.name):
            continue
        alias_groups[alias.address].append(alias)
    return canonical_by_address, alias_groups


def _should_suppress_byte_bitfields(register_name: str, fields: list[IniSfrField]) -> bool:
    width1 = [field for field in fields if field.width == 1]
    if not width1:
        return False
    expected = {f"{register_name}{bit}" for bit in range(8)}
    names = {field.name for field in width1}
    return names and names.issubset(expected)


def _canonical_bit_names(raw_name: str) -> list[str]:
    aliases = SPECIAL_BIT_NAME_ALIASES.get(raw_name)
    if aliases:
        return aliases
    return [_normalize_bit_name(raw_name)]


def _alias_bit_name(alias_register: str, base_register: str, base_bit_name: str) -> str:
    special = SPECIAL_ALIAS_BIT_BASE_NAMES.get((alias_register, base_bit_name))
    stem = special or base_bit_name
    if alias_register.endswith("1") and "1" in base_register:
        if stem.endswith("_") or stem[-1:].isdigit():
            return f"{stem}1" if stem.endswith("_") else f"{stem}_1"
        return f"{stem}1"
    return stem


def _should_emit_alias(metadata: DeviceMetadata, canonical_name: str, alias_name: str) -> bool:
    if metadata.ini_arch == "PIC14EX":
        return alias_name in PIC14EX_ALIAS_WHITELIST
    return True


def _render_bit_section(metadata: DeviceMetadata) -> list[str]:
    canonical_by_address, alias_groups = _build_register_groups(metadata)
    use_pragma_bit = (metadata.ini_arch or "").upper() == "PIC14"
    register_names = {sfr.address: sfr.name for sfr in canonical_by_address.values()}
    register_names.update(
        {
            0x0B: "INTCON",
        }
    )
    fields_by_address: dict[int, list[IniSfrField]] = {}
    for field in metadata.sfr_fields:
        fields_by_address.setdefault(field.address, []).append(field)

    blocked_names = {
        sfr.name
        for sfr in metadata.sfrs
        if sfr.width == 8 or sfr.width == 16
    }
    blocked_names.update(_predefined_bit_names(metadata))
    seen_names: set[str] = set()
    lines: list[str] = []
    for address, register_name in sorted(register_names.items()):
        fields = fields_by_address.get(address, [])
        if not fields:
            continue
        if address in PREDEFINED_FIELD_ADDRESSES:
            continue
        suppress_exploded = _should_suppress_byte_bitfields(register_name, fields)
        aliases = [alias for alias in alias_groups.get(address, []) if alias.name.endswith("1")]
        register_blocks: list[tuple[str, list[str]]] = [(register_name, [])]
        register_blocks.extend((alias.name, []) for alias in aliases)
        block_map = {name: block for name, block in register_blocks}
        for field in fields:
            if field.width != 1:
                continue
            if suppress_exploded and field.name.startswith(register_name):
                continue
            base_names = _canonical_bit_names(field.name)
            for base_name in base_names:
                if base_name in blocked_names:
                    continue
                if base_name not in seen_names:
                    block_map[register_name].append(
                        _render_bit_declaration(
                            name=base_name,
                            register_name=register_name,
                            address=address,
                            bit_position=field.bit_position,
                            use_pragma_bit=use_pragma_bit,
                        )
                    )
                    seen_names.add(base_name)
            for alias in aliases:
                alias_name = _alias_bit_name(alias.name, register_name, base_names[0])
                if alias_name in blocked_names:
                    continue
                if alias_name in seen_names:
                    continue
                block_map[alias.name].append(
                    _render_bit_declaration(
                        name=alias_name,
                        register_name=alias.name,
                        address=address,
                        bit_position=field.bit_position,
                        use_pragma_bit=use_pragma_bit,
                    )
                )
                seen_names.add(alias_name)
        for name, block in register_blocks:
            if not block:
                continue
            if lines:
                lines.append("")
            lines.extend(block)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _render_bit_declaration(
    name: str,
    register_name: str,
    address: int,
    bit_position: int,
    use_pragma_bit: bool,
) -> str:
    if use_pragma_bit:
        target = (
            f"/*OPTION_REG*/0x{address:02X}.{bit_position}"
            if register_name == "OPTION_REG"
            else f"{register_name}.{bit_position}"
        )
        return f"#pragma bit {name} @ {target}"
    return f"bit {name} @ {register_name}.{bit_position};"


def render_full_header(metadata: DeviceMetadata) -> str:
    lines = _render_header_prelude(metadata)
    sfr_lines = _render_sfr_section(metadata)
    if sfr_lines:
        lines.extend(sfr_lines)
        lines.append("")
        lines.append("")
    bit_lines = _render_bit_section(metadata)
    if bit_lines:
        lines.extend(bit_lines)
        lines.append("")
        lines.append("")
    lines.extend(render_dynamic_config_section(metadata).rstrip().splitlines())
    return "\n".join(lines).rstrip() + "\n"
