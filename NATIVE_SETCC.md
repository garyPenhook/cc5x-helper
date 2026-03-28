# Native `SETCC` Replacement Notes

`SETCC.EXE` is a Windows-only VCL GUI binary. Reverse engineering shows that its useful logic is split into:

- MPLAB metadata discovery for `<device>.ini` and `<device>.cfgdata`
- config/header generation
- source-file config block updates
- compiler launch orchestration

The bundled `tools/cc5x_setcc_native.py` replaces the scriptable parts cleanly on Linux without depending on the GUI.

The implementation direction should also consider Microchip technical brief `TB3261` in [getting_started_C.pdf](/home/gary/apps/cc5x_paid/getting_started_C.pdf). That brief is useful as a reference for modern PIC naming conventions and configuration practices, even though its examples target XC8-style headers rather than CC5X syntax.

## What It Covers

- `probe`
  - locates modern pack metadata first (`.PIC`, `.atdf`, pack `cfgdata`)
  - falls back to legacy MPLAB X `.ini/.cfgdata`
- `describe-device`
  - shows normalized device metadata parsed from the selected source
- `list-devices`
  - enumerates locally discoverable `PIC10F`/`PIC12F`/`PIC16F` devices from installed packs
- `doctor`
  - reports whether local packs, CrossOver, and `CC5X.EXE` are present and ready
- `project-init`
  - creates an open `setcc-native.json` manifest instead of opaque `setcc.pxk` state
- `project-validate`
  - validates that manifest before generation or build steps
- `project-edit-edition`
  - creates, copies, or deletes named editions in the project manifest
- `project-set-config`
  - sets or removes stored config-symbol values for a named edition
- `project-set-build-options`
  - replaces the stored build option list for a named edition
- `list-pack-config`
  - lists dynamic config symbols directly from pack metadata
- `render-pack-config`
  - emits a managed `#pragma config` block directly from pack metadata
- `render-pack-header`
  - emits a CC5X-style header skeleton from pack metadata
- `list-config`
  - extracts dynamic config symbols from shipped CC5X header files
- `render-config`
  - emits a managed `#pragma config` block
- `sync-config`
  - updates or appends that managed block in a source file
- `build`
  - launches `CC5X.EXE` directly, optionally through `wine`
- `tools/validate_generated_headers.py`
  - generates headers from pack metadata and compiles them with real `CC5X.EXE`
  - compares generated and shipped-header control cases for selected devices

## Why This Is A Better Direction

- It is Linux-native and scriptable.
- It avoids patching a closed-source Borland/VCL executable.
- It uses the dynamic config database already embedded in CC5X header files.
- It can target Microchip's modern pack layout instead of assuming old monolithic MPLAB installs.
- It makes config changes auditable in source control instead of hiding state in `setcc.pxk`.
- It does not require BKND to have shipped a header for a newer PIC device before the workflow is usable.
- It can use modern Microchip naming guidance as an input, then translate that into CC5X-style declarations instead of copying XC8 header mechanics directly.

## Examples

List dynamic config values from a header:

```bash
python3 tools/cc5x_setcc_native.py list-config \
  --header cc5x_paid/CC5X/16F1509.H
```

Render a config block:

```bash
python3 tools/cc5x_setcc_native.py render-config \
  --header cc5x_paid/CC5X/16F1509.H \
  --set FOSC=INTOSC \
  --set WDTE=OFF \
  --set MCLRE=ON
```

Update a source file in place:

```bash
python3 tools/cc5x_setcc_native.py sync-config \
  --source app.c \
  --header cc5x_paid/CC5X/16F1509.H \
  --set FOSC=INTOSC \
  --set WDTE=OFF
```

Update a source file from a project edition instead of ad hoc CLI state:

```bash
python3 tools/cc5x_setcc_native.py sync-config \
  --project setcc-native.json \
  --edition production
```

Probe MPLAB metadata:

```bash
python3 tools/cc5x_setcc_native.py probe --device PIC16F1509
```

List locally available CC5X-family devices from the installed packs:

```bash
python3 tools/cc5x_setcc_native.py list-devices --family 10F --family 16F
```

Check whether the local environment is ready for pack-driven generation and CrossOver-backed compile validation:

```bash
python3 tools/cc5x_setcc_native.py doctor
```

Initialize a checked-in project manifest:

```bash
python3 tools/cc5x_setcc_native.py project-init \
  --device PIC16F1509 \
  --main app.c
```

Validate that manifest:

```bash
python3 tools/cc5x_setcc_native.py project-validate
```

Create a new edition by copying `production`:

```bash
python3 tools/cc5x_setcc_native.py project-edit-edition \
  --edition qa \
  --copy-from production
```

Store config values in a named edition:

```bash
python3 tools/cc5x_setcc_native.py project-set-config \
  --edition production \
  --set FOSC=INTOSC \
  --set WDTE=OFF
```

Store edition-specific build flags:

```bash
python3 tools/cc5x_setcc_native.py project-set-build-options \
  --edition debug \
  --option=-a \
  --option=-k
```

List config values from the selected device pack instead of from a shipped BKND header:

```bash
python3 tools/cc5x_setcc_native.py list-pack-config --device PIC16F15313
```

Render a managed config block for a device that may not exist in the shipped CC5X header set:

```bash
python3 tools/cc5x_setcc_native.py render-pack-config \
  --device PIC16F15313 \
  --include-defaults \
  --set FEXTOSC=OFF \
  --set RSTOSC=HFINT32
```

Render a generated header from the selected device pack:

```bash
python3 tools/cc5x_setcc_native.py render-pack-header --device PIC16F15313
```

Validate generated headers with the real compiler under CrossOver:

```bash
PYTHONPATH=tools python3 tools/validate_generated_headers.py --json
```

Compile directly:

```bash
python3 tools/cc5x_setcc_native.py build \
  --runner wine \
  --compiler /path/to/CC5X.EXE \
  --main app.c \
  --option=-p16F1509 \
  --option=-Icc5x_paid/CC5X \
  --dry-run
```

Compile from the project manifest, with deterministic header generation and edition-specific flags:

```bash
python3 tools/cc5x_setcc_native.py build \
  --project setcc-native.json \
  --edition production \
  --dry-run
```

## Current Limits

- Header generation is a professional first pass, not full parity with every BKND hand-tuned header.
- Alias and alternate-bit-name policies still need device-by-device refinement.
- Modern Microchip guidance such as TB3261 must still be translated into CC5X conventions; not every XC8 naming or header pattern should be copied literally.
- It does not import the opaque `setcc.pxk` format.
- Header-based config workflows still assume a shipped CC5X header, but pack-native config workflows do not.
- The first project-manifest pass is intentionally narrow and covers the useful state from `setcc.pxk`, not every GUI preference.

## Current Validation State

The current generated headers compile successfully with the real CC5X compiler under CrossOver for:

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

Both generated and shipped-header control builds produce successful `.hex` and `.occ` outputs for the minimal validation source.

Current local source-availability note:

- the installed packs on this machine now give practical generation coverage for `10F`, `12F`, and `16F` devices

Those are still feasible next steps, but they are separate from the UI-heavy parts of `SETCC.EXE`.
