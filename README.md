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
- CrossOver/Wine-backed CC5X builds from Linux
- optional device programming through MPLAB IPECMD (PICkit 4/5, SNAP, ICD4)
- an optional desktop GUI and a VS Code extension

## Why This Exists

`SETCC.EXE` is a Windows-only Borland/VCL application. It hides project state in `setcc.pxk`, depends on Microchip metadata layouts that have changed over time, and is awkward to automate on Linux.

This project takes the modern route:

- prefer Microchip PIC DFP `.atpack` archives
- normalize `.PIC`, `.ini`, and `cfgdata` into one device model
- generate deterministic CC5X-style output
- keep project state in source control
- validate generated headers with the real `CC5X.EXE`

---

## Requirements

| Requirement | Needed for | Notes |
|-------------|-----------|-------|
| **Python ≥ 3.12** | everything | declared in `pyproject.toml` (`requires-python = ">=3.12"`) |
| **[uv](https://docs.astral.sh/uv/)** | recommended install/runner | manages the virtualenv, dependencies, and the bundled console scripts |
| **CC5X compiler (`CC5X.EXE`)** | `build`, header validation | proprietary, from B Knudsen Data (Norway). **Not bundled** — you supply it |
| **Wine or CrossOver** | running the Windows `CC5X.EXE` on Linux | invoked through a "runner" (see [Setup](#setup)) |
| **Microchip device packs** | device discovery, header/config generation | `.atpack` DFP archives, or an installed MPLAB X pack cache |
| **MPLAB X / IPECMD + a PICkit** | the optional `program` command | only needed if you flash hardware from this tool |
| **PyQt6 ≥ 6.6** | the optional desktop GUI | installed via the `gui` extra |

This repo does **not** bundle proprietary CC5X binaries or Microchip packs as tracked source files. You must obtain them separately and point the tool at them.

---

## Installation

### With uv (recommended)

```bash
git clone <this-repo> cc5x-helper
cd cc5x-helper

# Create the venv and install the project + runtime deps (e.g. defusedxml):
uv sync

# Include the GUI and/or dev (pytest, flake8) extras as needed:
uv sync --extra gui --extra dev
```

`uv sync` creates a project virtualenv and installs the two console entry points
declared in `pyproject.toml`:

- `cc5x-helper` → the CLI (`cc5x_setcc_native:main`)
- `cc5x-helper-gui` → the desktop GUI (requires the `gui` extra)

Run them through `uv run` (no manual venv activation needed):

```bash
uv run cc5x-helper doctor
uv run cc5x-helper list-devices --family 16F
uv run --extra gui cc5x-helper-gui
```

### One-off invocation without installing

`uv run` can fetch dependencies on the fly:

```bash
uv run --with defusedxml python tools/cc5x_setcc_native.py doctor
```

### With pip (alternative)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # base CLI
pip install -e ".[gui]"     # + desktop GUI
pip install -e ".[dev]"     # + pytest/flake8
```

> The examples below use `uv run cc5x-helper …`. If you installed with pip and
> activated the venv, drop the `uv run` prefix and just call `cc5x-helper …`.
> You can always fall back to `uv run python tools/cc5x_setcc_native.py …`.

---

## Setup

`cc5x-helper` discovers device metadata, the compiler, and a runner automatically,
with environment-variable overrides for anything in a non-default location. Run
`doctor` at any time to see what was found and what is still missing.

### 1. Provide the CC5X compiler

Obtain `CC5X.EXE` from B Knudsen Data and either:

- place it at the default path `cc5x_paid/CC5X/CC5X.EXE` inside the repo, **or**
- point at it explicitly with `CC5X_COMPILER`, or per-project with `--compiler`.

```bash
export CC5X_COMPILER="$HOME/tools/CC5X/CC5X.EXE"
```

### 2. Provide a runner (how the Windows compiler is launched)

The **runner** is the launcher that executes the Windows `CC5X.EXE` on Linux. It is a
command *template*: the compiler path is inserted wherever you write the `{compiler}`
placeholder. If the runner has no `{compiler}` placeholder it is treated as
**self-contained** — i.e. it already knows which compiler to run — and only the CC5X
options and the source file are appended. (This replaces an older rule that keyed off the
runner's filename, so renaming a wrapper no longer changes behavior.)

Minimal Wine runner — Wine needs the compiler path, so include the placeholder (a bare
`wine` runner without it fails with a clear "add the `{compiler}` placeholder" error):

```bash
uv run cc5x-helper build --project setcc-native.json --edition production --runner "wine {compiler}"
```

A pass-through wrapper script (forwards its arguments to Wine/CrossOver) also needs the
compiler, so invoke it with the placeholder, e.g. `~/apps/cc5x-run.sh {compiler}`:

```bash
#!/usr/bin/env bash
# Forward all CC5X arguments through Wine (or cxrun for a CrossOver bottle).
exec wine "$@"
# CrossOver example:
# exec /opt/cxoffice/bin/cxrun --bottle CC5X -- "$@"
```

```bash
chmod +x ~/apps/cc5x-run.sh
export CC5X_RUNNER="$HOME/apps/cc5x-run.sh {compiler}"
```

A **self-contained** wrapper that hard-codes the compiler path internally (like the
default `cc5x-run.sh`, which runs `'C:\Program Files\...\CC5X.EXE' "$@"`) needs **no**
placeholder — set the runner to just the script path.

The default runner path is `/home/gary/apps/cc5x-run.sh` and the default CrossOver
bottle is `~/.cxoffice/CC5X`; override both with the env vars or manifest fields if
your layout differs.

### 3. Provide device packs

Point the tool at Microchip device-pack metadata using any of:

- drop `Microchip.*.atpack` archives into `~/apps` or `~/Downloads` (scanned by default), **or**
- set `CC5X_ATPACK_DIRS` to a `:`-separated list of directories holding `.atpack` files, **or**
- rely on an installed MPLAB X pack cache (`~/.mchp_packs/Microchip`, or set `CC5X_PACK_ROOTS` / `MPLABX_PACKS`).

```bash
export CC5X_ATPACK_DIRS="$HOME/microchip/packs"
```

### 4. (Optional) Set up programming

To flash hardware with the `program` command you need MPLAB X's `ipecmd` and a
supported programmer (PICkit 4/5, SNAP, ICD4). IPECMD is auto-discovered under
installed MPLAB X versions, or set `CC5X_IPECMD`:

```bash
export CC5X_IPECMD="/opt/microchip/mplabx/v6.30/mplab_platform/mplab_ipe/ipecmd.sh"
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `CC5X_COMPILER` | Path to `CC5X.EXE` (overrides the default repo path) |
| `CC5X_RUNNER` | Launcher used to run the Windows compiler (e.g. `wine` or a wrapper script) |
| `CC5X_ATPACK_DIRS` | `:`-separated dirs containing `Microchip.*.atpack` archives |
| `CC5X_PACK_ROOTS` / `MPLABX_PACKS` | `:`-separated roots of unpacked `<family>/<version>/…` pack trees |
| `CC5X_IPECMD` | Path to `ipecmd.sh`/`ipecmd.exe` for the `program` command |

### Verify readiness

```bash
uv run cc5x-helper doctor
```

`doctor` reports discovered pack/device counts, the selected runner and compiler
(and whether they exist), CrossOver/Wine status, and an overall `ready` flag.

---

## Usage — CLI

Discover what is locally available:

```bash
uv run cc5x-helper list-devices --family 10F --family 16F
uv run cc5x-helper doctor
```

Inspect a device from the installed packs:

```bash
uv run cc5x-helper describe-device --device PIC16F1509
```

Generate a CC5X-style header straight from pack metadata:

```bash
uv run cc5x-helper render-pack-header --device PIC16F1509
```

Render a managed config block from pack metadata:

```bash
uv run cc5x-helper render-pack-config --device PIC16F1509 --include-defaults
```

### Project workflow

The checked-in replacement for `setcc.pxk` is `setcc-native.json` (see
[Manifest reference](#manifest-reference-setcc-nativejson)).

```bash
# 1. Create a manifest
uv run cc5x-helper project-init --device PIC16F1509 --main app.c

# 2. Validate it
uv run cc5x-helper project-validate

# 3. Sync the chosen edition's config into the source file
uv run cc5x-helper sync-config --project setcc-native.json --edition production

# 4. Build from the manifest
uv run cc5x-helper build --project setcc-native.json --edition production
```

Inspect and edit the manifest from the CLI:

```bash
uv run cc5x-helper project-list-editions
uv run cc5x-helper project-show --edition production
uv run cc5x-helper project-edit --header-mode existing --header-path include/16F1509.H
```

Add `--dry-run` to `build` to print the compiler command line without running it.

### Programming a device (optional)

```bash
# Program the device's hex image via a PICkit 4 (default tool)
uv run cc5x-helper program --project setcc-native.json --edition production

# Other operations and tools
uv run cc5x-helper program --action verify --device PIC16F1509 --hex build/app.hex --tool PK5
uv run cc5x-helper program --action erase --device PIC16F1509 --dry-run
```

`--action` accepts `program` / `verify` / `erase` / `blank-check`; `--tool` accepts
IPECMD `-TP` codes (`PK4`, `PK5`, `SNAP`, `ICD4`). Use `--ipe-arg` to pass raw
IPECMD flags (e.g. `--ipe-arg=-W2.5` to power the target) and `--dry-run` to preview
the command.

---

## Usage — Desktop GUI

```bash
uv run --extra gui cc5x-helper-gui
# or, from a packaged binary:
./dist/cc5x-helper-gui
```

Inside the GUI:

- `Help → Help Contents` or `F1` for the full new-user help system.
- `Help → Help For Current Tab` or `Shift+F1` for context help.
- Each main tab also has its own `Help` button, plus a persistent `Help` tab.

The GUI exposes the same workflow in three tabs:

- **Environment** — run `doctor` and browse local device-pack coverage
- **Devices** — probe/describe a device, list config symbols, render headers and config blocks
- **Projects** — create/load manifests, edit project fields, manage editions, sync config, launch builds

If a Linux desktop session forces an incompatible Qt platform theme:

```bash
QT_QPA_PLATFORM=xcb uv run --extra gui cc5x-helper-gui
```

---

## Usage — VS Code extension

A VS Code extension lives in [`vscode/cc5x-vscode`](vscode/cc5x-vscode). It builds and
programs projects by shelling out to this helper and to IPECMD.

Build and install it locally:

```bash
cd vscode/cc5x-vscode
npm install
npm run compile        # or: npx @vscode/vsce package  → cc5x-vscode-0.1.0.vsix
```

Then install the `.vsix` via *Extensions → … → Install from VSIX*, or run the
extension from VS Code's Extension Development Host (`F5`).

The extension activates when the workspace contains `setcc-native.json` and adds
the commands **CC5X: Doctor / Build / Program Device / Generate VS Code Tasks /
Refresh Artifacts**.

> **Workspace Trust required.** Because the extension runs the workspace-configured
> Python interpreter, helper script, and IPECMD, it only operates in
> [trusted workspaces](https://code.visualstudio.com/docs/editor/workspace-trust).
> In an untrusted folder the commands are disabled until you trust it.

Relevant settings (`cc5x.*`): `pythonPath`, `helperPath`, `manifest`,
`programmerTool`. You can also generate `.vscode/tasks.json` from the CLI:

```bash
uv run cc5x-helper vscode-tasks --project setcc-native.json --tool PK4
```

---

## Manifest reference (`setcc-native.json`)

`project-init` writes a manifest like this:

```json
{
  "version": 1,
  "device": "PIC16F1509",
  "compiler": "/path/to/CC5X/CC5X.EXE",
  "runner": "wine",
  "mplab_root": null,
  "header": { "mode": "generated", "path": "generated_headers/16F1509.H" },
  "config_source": "app.c",
  "main_source": "app.c",
  "build_options": [],
  "editions": {
    "debug":      { "config": {}, "build_options": [] },
    "production": { "config": {}, "build_options": [] }
  }
}
```

| Field | Meaning |
|-------|---------|
| `device` | Target PIC (e.g. `PIC16F1509`) |
| `compiler` | Path to `CC5X.EXE` (defaults from `CC5X_COMPILER`) |
| `runner` | Launcher prefix for the Windows compiler (e.g. `wine`) |
| `header.mode` | `generated` (pack-derived), `supplied`, or `existing` |
| `header.path` | Header file path used/produced for the build |
| `config_source` / `main_source` | File that receives synced config / the build entry point |
| `build_options` | Extra CC5X flags applied to every edition |
| `editions` | Named build variants, each with its own `config` map and `build_options` |

> The manifest can name arbitrary executables (`compiler`, `runner`), so treat it
> as trusted input — only build from manifests you control.

---

## Packaging — single-file Linux executables

Built with `uv` + PyInstaller:

```bash
bash tools/build_linux_executable.sh          # CLI  → dist/cc5x-helper
bash tools/build_linux_executable.sh gui      # GUI  → dist/cc5x-helper-gui
```

Equivalent direct commands:

```bash
uv run --python 3.13 --with pyinstaller pyinstaller \
  --onefile --name cc5x-helper --paths tools tools/cc5x_setcc_native.py

uv run --python 3.13 --with pyinstaller pyinstaller \
  --onefile --name cc5x-helper-gui --paths tools tools/cc5x_helper_gui.py
```

---

## Testing & validation

Unit tests (pytest is configured in `pyproject.toml`):

```bash
uv run --extra dev pytest
```

Lint:

```bash
uv run --extra dev flake8 tools tests
```

Real-compiler header validation (needs a working compiler + runner):

```bash
uv run python tools/validate_generated_headers.py --json
```

---

## Supported scope

This project targets CC5X-supported device families only. It does not claim PIC18 support.

Compiler-validated generated headers currently exist for:

- `PIC10F200`, `PIC10F320`, `PIC10F322`
- `PIC12F1501`, `PIC12F1840`
- `PIC16F1509`, `PIC16F15313`, `PIC16F1789`, `PIC16F18325`, `PIC16F18446`, `PIC16F18857`, `PIC16F19195`

Validation uses the real `CC5X.EXE` under CrossOver/Wine.

---

## Repository layout

- [tools](tools) — helper CLI, GUI, and library code
  - [packs.py](tools/cc5x_setcc_native_lib/packs.py) — pack discovery, version selection, device lookup
  - [picmeta.py](tools/cc5x_setcc_native_lib/picmeta.py) — normalized parsing of `.PIC`, `.ini`, `cfgdata`
  - [headergen.py](tools/cc5x_setcc_native_lib/headergen.py) — CC5X-style header and config-section generation
  - [project.py](tools/cc5x_setcc_native_lib/project.py) — `setcc-native.json` manifest model and validation
- [tests](tests) — unit tests for pack parsing and project manifests
- [vscode/cc5x-vscode](vscode/cc5x-vscode) — VS Code extension
- [NATIVE_SETCC.md](NATIVE_SETCC.md) — operator notes and command examples
- [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md) — validated devices and current project boundaries
- [CODEX_SETCC_IMPLEMENTATION_PLAN.md](CODEX_SETCC_IMPLEMENTATION_PLAN.md) — implementation plan and next phases

---

## Limitations

- Header generation is intentionally deterministic, but it is not yet full parity with every BKND hand-tuned header.
- Alias-heavy areas such as EUSART and MSSP still need additional compatibility refinement.
- The manifest workflow covers the useful persisted state from `setcc.pxk`, not every GUI preference.
- This repo does not bundle proprietary CC5X binaries or Microchip packs as tracked source files.
</content>
</invoke>
