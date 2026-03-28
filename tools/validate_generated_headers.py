#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from cc5x_setcc_native import find_device_metadata
from cc5x_setcc_native_lib.headergen import render_full_header
from cc5x_setcc_native_lib.picmeta import load_device_metadata


DEFAULT_DEVICES = [
    "PIC10F200",
    "PIC10F320",
    "PIC10F322",
    "PIC12F1501",
    "PIC12F1840",
    "PIC16F1509",
    "PIC16F15313",
    "PIC16F1789",
    "PIC16F18325",
    "PIC16F18446",
    "PIC16F18857",
    "PIC16F19195",
]
DEFAULT_RUNNER = Path("/home/gary/apps/cc5x-run.sh")
PROJECT_ROOT = Path("/home/gary/apps/cc5x_paid")
SHIPPED_HEADER_ROOT = PROJECT_ROOT / "cc5x_paid" / "CC5X"
VALIDATION_ROOT = PROJECT_ROOT / "validation" / "generated"


@dataclass(frozen=True)
class CompileResult:
    label: str
    device: str
    source_path: str
    header_path: str
    returncode: int
    succeeded: bool
    hex_exists: bool
    occ_exists: bool
    occ_summary: str | None
    stdout: str
    stderr: str


def to_windows_path(path: Path) -> str:
    return "Z:" + str(path.resolve()).replace("/", "\\")


def short_device_name(device: str) -> str:
    return device[3:] if device.upper().startswith("PIC") else device


def write_validation_source(source_path: Path, header_name: str) -> None:
    source_path.write_text(
        f'#include "{header_name}"\n'
        "void main(void) {\n"
        "}\n",
        encoding="ascii",
    )


def occ_summary(occ_path: Path) -> str | None:
    if not occ_path.exists():
        return None
    for line in occ_path.read_text(encoding="latin-1").splitlines():
        if line.startswith("Total of "):
            return line.strip()
    return None


def run_compile(runner: Path, include_dir: Path, source_path: Path, label: str, device: str) -> CompileResult:
    windows_include = to_windows_path(include_dir)
    windows_source = to_windows_path(source_path)
    completed = subprocess.run(
        [str(runner), "-S", f"-I{windows_include}", windows_source],
        cwd=source_path.parent,
        text=True,
        capture_output=True,
    )
    stem = source_path.with_suffix("")
    occ_path = stem.with_suffix(".occ")
    hex_path = stem.with_suffix(".hex")
    return CompileResult(
        label=label,
        device=device,
        source_path=str(source_path),
        header_path=str(include_dir / source_path.with_suffix(".H").name),
        returncode=completed.returncode,
        succeeded=completed.returncode == 0 and hex_path.exists(),
        hex_exists=hex_path.exists(),
        occ_exists=occ_path.exists(),
        occ_summary=occ_summary(occ_path),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def generate_device_header(device: str) -> str:
    result = find_device_metadata(device, None)
    metadata = load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )
    return render_full_header(metadata)


def validate_device(device: str, runner: Path) -> list[CompileResult]:
    short_name = short_device_name(device)
    device_dir = VALIDATION_ROOT / short_name
    generated_dir = device_dir / "generated"
    shipped_dir = device_dir / "shipped"
    generated_dir.mkdir(parents=True, exist_ok=True)
    shipped_dir.mkdir(parents=True, exist_ok=True)

    generated_header_path = generated_dir / f"{short_name}.H"
    generated_header_path.write_text(generate_device_header(device), encoding="latin-1")
    generated_source_path = generated_dir / f"{short_name.lower()}_gen.c"
    write_validation_source(generated_source_path, generated_header_path.name)
    generated_result = run_compile(
        runner=runner,
        include_dir=generated_dir,
        source_path=generated_source_path,
        label="generated",
        device=device,
    )

    results = [generated_result]
    shipped_header_path = SHIPPED_HEADER_ROOT / f"{short_name}.H"
    if shipped_header_path.exists():
        shipped_source_path = shipped_dir / f"{short_name.lower()}_shipped.c"
        write_validation_source(shipped_source_path, shipped_header_path.name)
        results.append(
            run_compile(
                runner=runner,
                include_dir=SHIPPED_HEADER_ROOT,
                source_path=shipped_source_path,
                label="shipped",
                device=device,
            )
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile generated CC5X headers under CrossOver to validate real compiler acceptance."
    )
    parser.add_argument("--device", action="append", dest="devices")
    parser.add_argument("--runner", default=str(DEFAULT_RUNNER))
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner = Path(args.runner)
    devices = args.devices or DEFAULT_DEVICES
    results: list[CompileResult] = []
    for device in devices:
        results.extend(validate_device(device, runner))
    if args.json:
        print(json.dumps([asdict(item) for item in results], indent=2))
    else:
        for item in results:
            status = "PASS" if item.succeeded else "FAIL"
            print(f"{status} {item.device} {item.label}")
            print(f"  source: {item.source_path}")
            print(f"  header: {item.header_path}")
            if item.occ_summary:
                print(f"  {item.occ_summary}")
            if item.stderr.strip():
                print("  stderr:")
                for line in item.stderr.strip().splitlines():
                    print(f"    {line}")
    return 0 if all(item.succeeded for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
