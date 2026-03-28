# cc5x-helper

`cc5x-helper` is a Linux-native replacement for the useful engineering workflows behind BKND's `SETCC.EXE`.

It is built for CC5X-target PIC families only:

- `PIC10F`
- `PIC12F`
- `PIC16F`

The tool does not try to recreate the old Windows GUI. It replaces the parts that matter in practice:

- device-pack discovery
- pack-driven CC5X header generation
- config-symbol discovery and managed `#pragma config` emission
- manifest-based project state instead of opaque `setcc.pxk`
- CrossOver-backed CC5X builds from Linux

## Why This Exists

`SETCC.EXE` is a Windows-only Borland/VCL application. It hides project state in `setcc.pxk`, depends on Microchip metadata layouts that have changed over time, and is awkward to automate on Linux.

This project takes the modern route:

- prefer Microchip PIC DFP `.atpack` archives
- normalize `.PIC`, `.ini`, and `cfgdata` into one device model
- generate deterministic CC5X-style output
- keep project state in source control
- validate generated headers with the real `CC5X.EXE`

## What It Uses

Primary metadata source priority:

1. PIC device pack archives in `/home/gary/apps/*.atpack`
2. unpacked Microchip pack caches such as `~/.mchp_packs/Microchip`
3. legacy MPLAB X `.ini/.cfgdata` installs

The project uses shipped BKND headers only as compatibility references and control cases. They are not required for pack-native workflows.

## Current Capabilities

Main CLI: [tools/cc5x_setcc_native.py](tools/cc5x_setcc_native.py)

Core commands:

- `probe`
- `describe-device`
- `list-devices`
- `doctor`
- `project-init`
- `project-validate`
- `project-edit`
- `project-edit-edition`
- `project-set-config`
- `project-set-build-options`
- `project-list-editions`
- `project-show`
- `list-pack-config`
- `render-pack-config`
- `render-pack-header`
- `list-config`
- `render-config`
- `sync-config`
- `build`

Library modules:

- [packs.py](tools/cc5x_setcc_native_lib/packs.py)
  - pack archive discovery, version selection, and device lookup
- [picmeta.py](tools/cc5x_setcc_native_lib/picmeta.py)
  - normalized parsing of `.PIC`, `.ini`, and `cfgdata`
- [headergen.py](tools/cc5x_setcc_native_lib/headergen.py)
  - CC5X-style header and config-section generation
- [project.py](tools/cc5x_setcc_native_lib/project.py)
  - `setcc-native.json` manifest model and validation

## Supported Scope

This project targets CC5X-supported device families only. It does not claim PIC18 support.

Compiler-validated generated headers currently exist for:

- `PIC10F200`
- `PIC10F320`
- `PIC10F322`
- `PIC12F1501`
- `PIC12F1840`
- `PIC16F1509`
- `PIC16F15313`
- `PIC16F1789`
- `PIC16F18325`
- `PIC16F18446`
- `PIC16F18857`
- `PIC16F19195`

Validation uses the real `CC5X.EXE` under CrossOver.

## Repository Layout

- [tools](tools)
  - helper CLI and library code
- [tests](tests)
  - unit tests for pack parsing and project manifests
- [NATIVE_SETCC.md](NATIVE_SETCC.md)
  - operator notes and command examples
- [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md)
  - validated devices and current project boundaries
- [CODEX_SETCC_IMPLEMENTATION_PLAN.md](CODEX_SETCC_IMPLEMENTATION_PLAN.md)
  - implementation plan and next phases

## Quick Start

List locally discoverable devices:

```bash
python3 tools/cc5x_setcc_native.py list-devices --family 10F --family 16F
```

Check whether the local toolchain is ready:

```bash
python3 tools/cc5x_setcc_native.py doctor
```

Inspect a device from the installed packs:

```bash
python3 tools/cc5x_setcc_native.py describe-device --device PIC16F1509
```

Generate a CC5X-style header from the pack:

```bash
python3 tools/cc5x_setcc_native.py render-pack-header --device PIC16F1509
```

Render a managed config block directly from pack metadata:

```bash
python3 tools/cc5x_setcc_native.py render-pack-config \
  --device PIC16F1509 \
  --include-defaults
```

## Project Workflow

The first-pass replacement for `setcc.pxk` is `setcc-native.json`.

Create a project manifest:

```bash
python3 tools/cc5x_setcc_native.py project-init \
  --device PIC16F1509 \
  --main app.c
```

Validate the manifest:

```bash
python3 tools/cc5x_setcc_native.py project-validate
```

Sync config from the named edition into the source file:

```bash
python3 tools/cc5x_setcc_native.py sync-config \
  --project setcc-native.json \
  --edition production
```

Build from the manifest:

```bash
python3 tools/cc5x_setcc_native.py build \
  --project setcc-native.json \
  --edition production
```

Inspect and edit the manifest from the CLI:

```bash
python3 tools/cc5x_setcc_native.py project-list-editions
python3 tools/cc5x_setcc_native.py project-show --edition production
python3 tools/cc5x_setcc_native.py project-edit \
  --header-mode existing \
  --header-path include/16F1509.H
```

## Linux Executable

This repo can be packaged into a single-file Linux executable with `uv` and PyInstaller, using the active Python 3.13 toolchain on this machine.

Build it:

```bash
bash tools/build_linux_executable.sh
```

The resulting binary is written to:

```bash
dist/cc5x-helper
```

Equivalent direct command:

```bash
uv run --python 3.13 --with pyinstaller pyinstaller \
  --onefile \
  --name cc5x-helper \
  --paths tools \
  tools/cc5x_setcc_native.py
```

## Validation

Unit tests:

```bash
PYTHONPATH=tools python3 -m unittest tests/test_packs.py tests/test_project.py
```

Real compiler validation:

```bash
PYTHONPATH=tools python3 tools/validate_generated_headers.py --json
```

## Limitations

- Header generation is intentionally deterministic, but it is not yet full parity with every BKND hand-tuned header.
- Alias-heavy areas such as EUSART and MSSP still need additional compatibility refinement.
- The manifest workflow covers the useful persisted state from `setcc.pxk`, not every GUI preference.
- This repo does not bundle proprietary CC5X binaries or Microchip packs as tracked source files.
