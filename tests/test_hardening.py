"""Regression tests for pack-parsing / header-generation hardening."""
from __future__ import annotations

from pathlib import Path

import pytest

from cc5x_setcc_native_lib.headergen import _safe_comment, _safe_identifier
from cc5x_setcc_native_lib.packs import (
    _read_text_file_capped,
    read_text_reference,
)


def test_safe_comment_strips_backslash_line_splice() -> None:
    # A trailing backslash would splice the next generated line into the // comment.
    assert "\\" not in _safe_comment("clock select\\")
    assert "\n" not in _safe_comment("line1\nline2")
    assert _safe_comment("  spaced   out  ") == "spaced out"


def test_safe_identifier_neutralizes_injection() -> None:
    assert _safe_identifier("ADGO") == "ADGO"
    assert "\n" not in _safe_identifier("evil\nchar x @ 0")
    assert " " not in _safe_identifier("a b c")
    assert _safe_identifier("9live")[0] == "_"


def test_read_text_file_capped_rejects_oversized(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * 100)
    with pytest.raises(ValueError, match="oversized"):
        _read_text_file_capped(big, "utf-8", max_bytes=10)


def test_read_text_file_capped_rejects_non_regular_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(ValueError, match="not a regular file"):
        _read_text_file_capped(missing, "utf-8")


def test_read_text_reference_reads_plain_file(tmp_path: Path) -> None:
    src = tmp_path / "device.ini"
    src.write_text("[dev]\nARCH=PIC14E\n", encoding="utf-8")
    assert "ARCH=PIC14E" in read_text_reference(str(src))
