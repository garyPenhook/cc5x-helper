from __future__ import annotations

from pathlib import Path

import validate_generated_headers as validator
from cc5x_setcc_native_lib import picmeta as P
from validate_generated_headers import normalize_pic_device_name, shipped_header_devices


def test_normalize_pic_device_name_accepts_short_or_full_name() -> None:
    assert normalize_pic_device_name("10f200") == "PIC10F200"
    assert normalize_pic_device_name("PIC16F1509") == "PIC16F1509"


def test_shipped_header_devices_lists_flash_pic_headers(tmp_path: Path) -> None:
    for name in (
        "10F200.H",
        "12F1840.H",
        "16F1509.H",
        "16f15313.h",
        "MATH16.H",
        "INLINE.H",
        "12C508.H",
        "16C84.H",
        "16HV610.H",
        "README.TXT",
    ):
        (tmp_path / name).write_text("// header\n", encoding="latin-1")

    assert shipped_header_devices(tmp_path) == [
        "PIC10F200",
        "PIC12F1840",
        "PIC16F1509",
        "PIC16F15313",
    ]


def test_validate_device_reports_generation_failure(monkeypatch, tmp_path: Path) -> None:
    def fail_generate(_device: str) -> str:
        raise ValueError("unsupported metadata")

    monkeypatch.setattr(validator, "VALIDATION_ROOT", tmp_path / "validation")
    monkeypatch.setattr(validator, "SHIPPED_HEADER_ROOT", tmp_path / "missing_headers")
    monkeypatch.setattr(validator, "generate_device_header", fail_generate)

    results = validator.validate_device("PIC12F1552", runner=["unused"], timeout=1)

    assert len(results) == 1
    assert results[0].label == "generated"
    assert not results[0].succeeded
    assert "unsupported metadata" in results[0].stderr


def test_generate_device_header_fails_when_pack_metadata_is_missing(monkeypatch, tmp_path: Path) -> None:
    shipped_root = tmp_path / "headers"
    shipped_root.mkdir()
    (shipped_root / "12F1552.H").write_text("// BKND shipped header\n", encoding="latin-1")

    def fake_find(_device: str, _mplab_root: str | None) -> dict[str, str | None]:
        return {
            "device": "PIC12F1552",
            "pack_ini": None,
            "ini": None,
            "pack_cfgdata": None,
            "cfgdata": None,
            "pic": None,
        }

    def fake_load(**_kwargs) -> P.DeviceMetadata:
        return P.DeviceMetadata(
            device="PIC12F1552",
            ini_arch=None,
            ini_procid=None,
            rom_size_words=None,
            banks=None,
            bank_size=None,
            sfr_count=0,
            sfr_field_count=0,
            config_word_count=0,
            config_setting_count=0,
            config_value_count=0,
            config_words=[],
            sfrs=[],
            sfr_fields=[],
            ram_ranges=[],
            common_ranges=[],
            icd_ram_ranges=[],
            pic_summary=None,
        )

    monkeypatch.setattr(validator, "SHIPPED_HEADER_ROOT", shipped_root)
    monkeypatch.setattr(validator, "find_device_metadata", fake_find)
    monkeypatch.setattr(validator, "load_device_metadata", fake_load)

    try:
        validator.generate_device_header("PIC12F1552")
    except validator.MissingPackMetadataError as exc:
        assert "no pack metadata" in str(exc)
    else:
        raise AssertionError("missing pack metadata must not fall back to shipped header")


def test_all_shipped_success_policy_ignores_shipped_control_failures() -> None:
    generated = validator.CompileResult(
        label="generated",
        device="PIC16F1703",
        source_path="gen.c",
        header_path="gen.H",
        returncode=0,
        succeeded=True,
        hex_exists=True,
        occ_exists=True,
        occ_summary=None,
        stdout="",
        stderr="",
    )
    shipped = validator.CompileResult(
        label="shipped",
        device="PIC16F1703",
        source_path="ship.c",
        header_path="16F1703.H",
        returncode=1,
        succeeded=False,
        hex_exists=False,
        occ_exists=True,
        occ_summary=None,
        stdout="BKND typo",
        stderr="",
    )

    assert not validator.validation_succeeded([generated, shipped])
    assert validator.validation_succeeded([generated, shipped], generated_only=True)
    assert not validator.validation_succeeded([shipped], generated_only=True)
