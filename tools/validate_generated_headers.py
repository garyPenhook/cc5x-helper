#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from cc5x_setcc_native import (
    DEFAULT_COMPILER,
    DEFAULT_RUNNER,
    atomic_write_text,
    find_device_metadata,
)
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
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 300.0
PROJECT_ROOT = Path(__file__).resolve().parent.parent
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


def default_runner_spec() -> str:
    return os.environ.get("CC5X_RUNNER") or str(DEFAULT_RUNNER)


def runner_command(runner_spec: str) -> list[str]:
    parts = shlex.split(runner_spec)
    if not parts:
        raise SystemExit("--runner must not be empty")
    return [part.replace("{compiler}", str(DEFAULT_COMPILER)) for part in parts]


def timeout_seconds(value: float) -> float | None:
    return value if value > 0 else None


def timeout_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def occ_summary(occ_path: Path) -> str | None:
    if not occ_path.exists():
        return None
    for line in occ_path.read_text(encoding="latin-1").splitlines():
        if line.startswith("Total of "):
            return line.strip()
    return None


def run_compile(
    runner: list[str],
    include_dir: Path,
    source_path: Path,
    header_path: Path,
    label: str,
    device: str,
    timeout: float | None,
) -> CompileResult:
    windows_include = to_windows_path(include_dir)
    windows_source = to_windows_path(source_path)
    stem = source_path.with_suffix("")
    occ_path = stem.with_suffix(".occ")
    hex_path = stem.with_suffix(".hex")
    try:
        completed = subprocess.run(
            [*runner, "-S", f"-I{windows_include}", windows_source],
            cwd=source_path.parent,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = timeout_output(exc.stdout)
        stderr = (
            timeout_output(exc.stderr)
            + f"\ncompile timed out after {exc.timeout:g} seconds"
        ).lstrip()
    return CompileResult(
        label=label,
        device=device,
        source_path=str(source_path),
        header_path=str(header_path),
        returncode=returncode,
        succeeded=returncode == 0 and hex_path.exists(),
        hex_exists=hex_path.exists(),
        occ_exists=occ_path.exists(),
        occ_summary=occ_summary(occ_path),
        stdout=stdout,
        stderr=stderr,
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


def validate_device(device: str, runner: list[str], timeout: float | None) -> list[CompileResult]:
    short_name = short_device_name(device)
    device_dir = VALIDATION_ROOT / short_name
    generated_dir = device_dir / "generated"
    shipped_dir = device_dir / "shipped"
    generated_dir.mkdir(parents=True, exist_ok=True)
    shipped_dir.mkdir(parents=True, exist_ok=True)

    generated_header_path = generated_dir / f"{short_name}.H"
    atomic_write_text(
        generated_header_path,
        generate_device_header(device),
        encoding="latin-1",
    )
    generated_source_path = generated_dir / f"{short_name.lower()}_gen.c"
    write_validation_source(generated_source_path, generated_header_path.name)
    generated_result = run_compile(
        runner=runner,
        include_dir=generated_dir,
        source_path=generated_source_path,
        header_path=generated_header_path,
        label="generated",
        device=device,
        timeout=timeout,
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
                header_path=shipped_header_path,
                label="shipped",
                device=device,
                timeout=timeout,
            )
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile generated CC5X headers under CrossOver to validate real compiler acceptance."
    )
    parser.add_argument("--device", action="append", dest="devices")
    parser.add_argument("--runner", default=default_runner_spec())
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        help=(
            "Abort each compiler invocation after this many seconds "
            "(0 disables; default: 300)."
        ),
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner = runner_command(args.runner)
    compile_timeout = timeout_seconds(args.timeout_seconds)
    devices = args.devices or DEFAULT_DEVICES
    results: list[CompileResult] = []
    for device in devices:
        results.extend(validate_device(device, runner, compile_timeout))
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
