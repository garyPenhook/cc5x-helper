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
from cc5x_setcc_native_lib import debuggen, measure
from cc5x_setcc_native_lib.headergen import render_full_header
from cc5x_setcc_native_lib.picmeta import load_device_metadata
from cc5x_setcc_native_lib.project import load_project_file

# Full compile flags for the measure gate: -V emits the variable list (<src>.var) and
# -Q the call tree (<src>.fcs); the .occ budget file is written by default (CC5X 3.8
# manual, cc5x_paid/cc5x-38.pdf, options -V/-Q). These flags REPLACE the normal -S
# (silent): -S together with both -V and -Q is a CC5X bug that aborts with "Error
# opening file <src>.occ" (verified empirically on CC5X 3.8A); -V -Q alone works.
# Override with CC5X_REPORT_FLAGS for a different toolchain version.
CC5X_REPORT_FLAGS = shlex.split(os.environ.get("CC5X_REPORT_FLAGS", "-V -Q"))


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
    compile_flags: list[str] = ("-S",),
) -> CompileResult:
    windows_include = to_windows_path(include_dir)
    windows_source = to_windows_path(source_path)
    stem = source_path.with_suffix("")
    occ_path = stem.with_suffix(".occ")
    hex_path = stem.with_suffix(".hex")
    try:
        completed = subprocess.run(
            [*runner, *compile_flags, f"-I{windows_include}", windows_source],
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


def _metadata_for(device: str):
    result = find_device_metadata(device, None)
    return load_device_metadata(
        device=result["device"],
        ini_reference=result.get("pack_ini") or result.get("ini"),
        cfgdata_reference=result.get("pack_cfgdata") or result.get("cfgdata"),
        pic_reference=result.get("pic"),
    )


# Public entry points a generated stub may expose, with a measurement call for each.
# Only those actually declared in the stub header are referenced (lower tiers omit
# some), so the harness compiles at every tier without touching an undefined symbol.
_STUB_ENTRY_CALLS = {
    "cdl_init": "cdl_init();",
    "cdl_trace": "cdl_trace(0, 0);",
    "cdl_poll": "cdl_poll();",
    "cdl_bp": "cdl_bp(0);",
    # Toggle-tier marker: without a call, CC5X can dead-strip the pulse code and the
    # .occ would under-measure the toggle stub. Only emitted when the header declares
    # it (full/min stubs do not), so adding it is safe for every tier.
    "cdl_mark": "cdl_mark(0);",
}


def _measurement_harness(device_header_name: str, stub_c_name: str, stub_h_text: str) -> str:
    """A standalone TU that makes the stub fragment measurable: pull in the device header
    (SFRs + CC5X types), then the stub source, then a main() that calls every public
    entry point the stub header declares so CC5X keeps (and budgets) all of them."""
    calls = [call for fn, call in _STUB_ENTRY_CALLS.items() if f"{fn}(" in stub_h_text]
    body = "\n".join(f"    {c}" for c in calls)
    return (
        f'// Generated measurement harness (P2 gate) -- DO NOT EDIT.\n'
        f'#include "{device_header_name}"\n'
        f'#include "{stub_c_name}"\n'
        f'void main(void) {{\n{body}\n}}\n'
    )


def measure_debug_stub(
    device: str,
    runner: list[str],
    timeout: float | None,
    debug_config: dict,
    *,
    report_flags: list[str] = CC5X_REPORT_FLAGS,
    hw_stack_depth: int | None = None,
) -> debuggen.GateResult:
    """Run the 02 §6 measured-budget gate for a device's debug stub end-to-end:
    pick the provisional tier, then compile-and-measure down the ladder until a tier's
    real CC5X budget fits (or the floor fails), and write the confirmed stub + map with
    the measured symbols/budget folded in.

    ``debug_config`` is the project's full ``debug`` section: it must carry everything a
    monitor-tier stub needs to even *generate* -- most importantly ``transport.brg`` (the
    Fosc-dependent SPBRG value, which is never guessed). Each tier the gate tries is the
    base config with only ``tier`` overridden, so a demote re-measures the same project
    config at a lighter tier. Generating without ``brg`` would raise immediately for any
    full/min device, so the gate cannot run on a bare ``{"tier": ...}``.

    The compile step is the only part not covered by the unit suite (it needs the real
    CrossOver CC5X compiler); the tier loop, parsing, and map-fill are all tested in
    test_measure.py via injected fixtures.
    """
    metadata = _metadata_for(device)
    short = short_device_name(device)
    build_dir = VALIDATION_ROOT / short / "debug-stub"
    build_dir.mkdir(parents=True, exist_ok=True)

    device_header = generate_device_header(device)

    def gen_for(tier: str) -> debuggen.GeneratedDebug:
        # Override only the tier; keep brg/channels/pins/breakpoints from the project.
        return debuggen.generate_debug_stub(metadata, {**debug_config, "tier": tier})

    def measure_at(tier: str) -> debuggen.Measurement:
        # The stub is a *fragment*: no main, and it relies on the device header for SFRs
        # and CC5X types. Wrap it in a harness TU (device header + stub + a main that
        # touches every public entry point so none is dead-stripped) -- this is the
        # p2stub.c pattern the golden fixtures were produced with.
        gen = gen_for(tier)
        short = short_device_name(device)
        atomic_write_text(build_dir / f"{short}.H", device_header, encoding="latin-1")
        atomic_write_text(build_dir / gen.monitor_h_name, gen.monitor_h, encoding="latin-1")
        atomic_write_text(build_dir / gen.monitor_c_name, gen.monitor_c, encoding="latin-1")
        harness = _measurement_harness(f"{short}.H", gen.monitor_c_name, gen.monitor_h)
        src = build_dir / "measure_main.c"
        atomic_write_text(src, harness, encoding="latin-1")
        res = run_compile(
            runner=runner, include_dir=build_dir, source_path=src,
            header_path=build_dir / gen.monitor_h_name, label=f"debug-stub:{tier}",
            device=device, timeout=timeout, compile_flags=report_flags,
        )
        if not res.succeeded:
            raise RuntimeError(
                f"{device}: debug-stub compile failed at tier {tier!r} "
                f"(rc={res.returncode})\n{(res.stderr or res.stdout).strip()}")
        return measure.read_reports(build_dir, src.stem)

    # Honor a manifest-forced tier as the provisional: hardcoding "auto" here would
    # re-auto-select and measure/emit the higher auto tier even when the project
    # intentionally forced trace/toggle. gen_for already pins {**debug_config, tier},
    # so pass the configured tier (default "auto" when unset). The gate is force-down
    # only, so a forced tier is still demoted if its measured budget overruns.
    provisional = gen_for(debug_config.get("tier") or "auto")
    result = debuggen.run_measure_gate(
        provisional.decision, provisional.caps, measure_at, hw_stack_depth=hw_stack_depth)

    # Regenerate the stub/map at the final tier and fold in the matching measurement,
    # so the shipped map carries the confirmed tier + measured symbols/budget (02 §6).
    final = gen_for(result.decision.tier)
    map_payload = json.loads(final.map_json)
    debuggen.apply_measurement(map_payload, result.decision, result.occ, result.varsyms)
    atomic_write_text(build_dir / final.monitor_h_name, final.monitor_h, encoding="latin-1")
    atomic_write_text(build_dir / final.monitor_c_name, final.monitor_c, encoding="latin-1")
    atomic_write_text(build_dir / final.map_name, json.dumps(map_payload, indent=2) + "\n",
                      encoding="latin-1")
    return result


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
    parser.add_argument(
        "--debug-stub",
        action="store_true",
        help="Run the 02 §6 measured-budget gate on each device's CDL debug stub "
             "(compile-and-measure, confirm/demote tier, write the measured map) "
             "instead of validating the plain device header.",
    )
    parser.add_argument(
        "--project",
        help="Project manifest (setcc-native.json) whose 'debug' section supplies the "
             "stub config (transport.brg, channels, pins) for --debug-stub. Required "
             "for monitor tiers, which cannot be generated without a baud divisor.",
    )
    return parser


def _debug_config_from_project(project_path: str | None) -> dict:
    """Load the manifest 'debug' section for the gate. Raises ValueError (-> clean exit)
    when there is no usable config, since a monitor-tier stub cannot be generated
    without one (notably transport.brg)."""
    if not project_path:
        raise ValueError("--debug-stub requires --project <manifest> to supply debug.transport.brg")
    debug = load_project_file(Path(project_path)).debug
    if not debug:
        raise ValueError(f"{project_path}: no 'debug' section to drive the measured-budget gate")
    return debug


def _run_debug_stub_gate(devices: list[str], runner: list[str], timeout: float | None,
                         as_json: bool, project_path: str | None) -> int:
    try:
        debug_config = _debug_config_from_project(project_path)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if as_json else f"error: {exc}")
        return 2
    payloads = []
    ok = True
    for device in devices:
        try:
            result = measure_debug_stub(device, runner, timeout, debug_config)
        except Exception as exc:  # compile failure / ungeneratable tier -> per-device FAIL, not a crash
            ok = False
            payloads.append({"device": device, "confirmed": False, "error": str(exc)})
            continue
        ok = ok and result.confirmed
        payloads.append({
            "device": device,
            "tier": result.decision.tier,
            "confirmed": result.confirmed,
            "reason": result.decision.reason,
            "history": list(result.history),
        })
    if as_json:
        print(json.dumps(payloads, indent=2))
    else:
        for p in payloads:
            if "error" in p:
                print(f"FAIL {p['device']}: {p['error']}")
                continue
            print(f"{'CONFIRM' if p['confirmed'] else 'FAIL'} {p['device']} -> tier {p['tier']}")
            for step in p["history"]:
                print(f"    {step}")
    return 0 if ok else 1


def main() -> int:
    args = build_parser().parse_args()
    runner = runner_command(args.runner)
    compile_timeout = timeout_seconds(args.timeout_seconds)
    devices = args.devices or DEFAULT_DEVICES
    if args.debug_stub:
        return _run_debug_stub_gate(devices, runner, compile_timeout, args.json, args.project)
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
