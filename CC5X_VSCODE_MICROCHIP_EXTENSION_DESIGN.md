# CC5X Integration for VS Code and Microchip MPLAB Extensions

## Executive Summary

The practical path is a companion VS Code extension named `cc5x-vscode` that wraps the existing `cc5x-helper` Python library and command-line workflow, while interoperating with Microchip's MPLAB extensions through project files, VS Code tasks, launch/program tasks, generated headers, pack metadata, and optional MPLAB project JSON.

Do not start by modifying Microchip's shipped extensions. On this machine the installed Microchip extensions are packaged/minified VSIX artifacts, and the public extension surfaces are commands, tasks, settings, `.mplab.json` project files, and debug/program tasks. There is no documented public API for registering CC5X as a first-class MPLAB toolchain provider equivalent to XC8, AVR-GCC, ARM-GCC, XC16, or XC32.

The correct design is therefore layered:

1. Provide a reliable CC5X workspace experience in VS Code.
2. Generate files that Microchip extensions already understand where possible.
3. Use Microchip debug/programming tasks when a generated image can be consumed.
4. Research internal toolchain registration only as a later experimental phase.

This preserves CC5X as its own compiler family rather than pretending it is XC8.

## Current Local Baseline

This repository already provides the most important back end pieces:

- `tools/cc5x_setcc_native.py`
  - device probing
  - pack-based metadata discovery
  - header rendering
  - config block rendering and sync
  - `setcc-native.json` project manifests
  - manifest-driven builds
- `tools/cc5x_setcc_native_lib/`
  - pack discovery
  - normalized PIC metadata
  - header generation
  - project manifest validation
- `tools/cc5x_helper_gui.py`
  - PyQt6 GUI over the same workflow
- `SUPPORT_MATRIX.md`
  - compiler-validated devices across PIC10F, PIC12F, and PIC16F
- `setcc-native.json`
  - current open manifest format, replacing opaque `setcc.pxk`

The extension should reuse this library. It should not duplicate pack parsing, config rendering, or compiler invocation logic in TypeScript.

## External Research Findings

### Microchip MPLAB Extensions

Microchip describes MPLAB Tools for VS Code as a collection of extensions combining the MPLAB ecosystem with VS Code. The extension pack currently includes user interface, MCC, project importer, debug adapter, data visualizer, memory inspector, and CMake runner components.

Observed official/documented behavior:

- MPLAB projects are created/opened through commands such as `MPLAB: Create New Project`.
- Project properties can be edited through `MPLAB: Edit project properties`.
- Builds are run through `MPLAB CMake: Build`, clean, and related commands.
- Debugging starts through VS Code debug UX or Microchip commands.
- MCC uses separate generated-file layouts in VS Code compared with MPLAB X.
- Existing MPLAB X projects can be imported into VS Code, but VS Code MPLAB projects cannot be imported back into MPLAB X.
- MPLAB Debug Adapter supports VS Code tasks for `program`, `erase`, and GDB server start.

Observed local extension versions:

- `microchip.mplab-extension-pack` 1.2.3
- `microchip.mplab-extensions-core` 2.0.5
- `microchip.mplab-ui` 1.7.3
- `microchip.runcmake` 1.7.1
- `microchip.toolchains` 1.7.3
- `microchip.mplab-core-da` 1.8.2
- `microchip.mplab-clangd` 1.7.0
- `microchip.mplabx-importer` 1.7.0
- `microchip.mplab-code-configurator` 0.1.6
- `microchip.mplab-data-visualizer` 0.1.4

Important local manifest facts:

- `microchip.runcmake` contributes task type `MPLAB-CMake`.
- `microchip.mplab-core-da` contributes task types `MPLAB-DebugAdapter` and `MPLAB-GdbServer`.
- `microchip.toolchains` exposes only user-facing commands/settings such as `mplab.addToolchains`, `mplab.listToolchains`, and `mplab.toolchains.customPaths`.
- `.mplab.json` files describe configurations, device, packs, file sets, image path, and a toolchain property group provider such as `microchip.toolchains:avr-gcc@15.2.0` or `microchip.toolchains:arm-gcc@14.2.1`.

### VS Code Extension APIs

Relevant official VS Code surfaces:

- A Task Provider can auto-detect project files and contribute tasks.
- A task can run a direct process using `ProcessExecution`, avoiding shell quoting problems.
- Custom tasks are appropriate for build systems with saved state or custom output handling, but they require implementing a pseudoterminal.
- C/C++ IntelliSense can be driven by `compile_commands.json` or a custom configuration provider.
- The Microsoft C/C++ extension does not include a compiler or debugger; it expects external command-line tools.
- Language servers are the right architecture when analysis becomes heavy or needs to run outside the extension host.

### CC5X-Specific Constraints

CC5X cannot be treated as XC8:

- Device selection uses `-p<device>`, `#pragma chip`, or CC5X headers.
- Edition differences affect language and library features.
- Defaults differ from modern hosted C:
  - unsigned `char`
  - 8-bit signed `int`
  - 16-bit signed `long`
  - static local and parameter allocation
  - no automatic RAM initialization
- Generated artifacts are part of normal diagnosis:
  - `.asm`
  - `.hex`
  - `.occ`
  - `.var`
  - `.fcs`
  - `.cpr`
  - `.cod`
  - `.cof`
- MPLINK/relocatable flows, interrupts, banking, page selection, inline assembly, and manual layout need CC5X-aware validation.

## Design Goals

Primary goals:

- Make a CC5X project usable directly in VS Code on Linux.
- Keep Microchip device-pack metadata as the authoritative source for modern PIC10F/PIC12F/PIC16F devices.
- Provide build, clean, config sync, header generation, device probe, and artifact inspection commands.
- Surface compiler diagnostics in VS Code Problems.
- Provide IntelliSense that is useful despite CC5X's nonstandard dialect.
- Generate optional Microchip-compatible `.mplab.json` and VS Code tasks for programming/debugging workflows.
- Keep project state in source-controlled text files.

Non-goals for first release:

- Reimplementing MPLAB X.
- Recreating `SETCC.EXE` visually.
- Modifying Microchip's shipped extensions.
- Claiming full first-class Microchip toolchain-provider status before a supported or stable internal API is identified.
- Pretending CC5X produces complete modern ELF/DWARF debug output for all debugger features.

## User Experience

Commands contributed by `cc5x-vscode`:

- `CC5X: Doctor`
- `CC5X: Create Project`
- `CC5X: Open Project Manifest`
- `CC5X: Select Device`
- `CC5X: Describe Device`
- `CC5X: Generate Header`
- `CC5X: Render Config`
- `CC5X: Sync Config Block`
- `CC5X: Build`
- `CC5X: Clean`
- `CC5X: Validate Project`
- `CC5X: Show Build Artifacts`
- `CC5X: Generate VS Code Tasks`
- `CC5X: Generate MPLAB Project Sidecar`
- `CC5X: Generate Microchip Program Task`

Views:

- CC5X Projects
  - loaded manifests
  - device
  - active edition
  - main source
  - header mode
  - last build result
- CC5X Device
  - pack source
  - config symbols
  - register/header generation status
- CC5X Artifacts
  - `.hex`
  - `.asm`
  - `.occ`
  - `.var`
  - `.fcs`
  - `.cpr`
  - `.cod`/`.cof` when present

Editor contributions:

- CodeLens over managed config block:
  - sync from manifest
  - edit config values
  - show legal values
- Hover for config symbols and register names when known from pack metadata.
- Diagnostics for manifest errors, unknown config symbols, invalid config values, missing generated header, missing compiler runner, and stale build artifacts.

## Architecture

### Components

#### VS Code Extension Front End

Language: TypeScript.

Responsibilities:

- Discover `setcc-native.json`.
- Contribute commands, views, task provider, diagnostics, status bar items, and webviews.
- Call the Python CLI using `ProcessExecution` or Node child processes.
- Parse machine-readable JSON output from the helper.
- Write workspace files only through explicit commands or confirmed project creation steps.
- Generate `.vscode/tasks.json`, `.vscode/launch.json`, and optional `.vscode/<project>.mplab.json`.

#### CC5X Helper Back End

Language: Python, using this repository's existing code.

Required additions:

- Stable JSON output for every extension-facing command.
- Stable exit codes.
- `--workspace-root` handling.
- `--project` default discovery.
- `build --json-diagnostics`.
- `artifacts --json`.
- `doctor --json`.
- `vscode-generate --tasks-json`.
- `mplab-generate --project-json`.

The extension should shell out to the CLI at first. Direct Python embedding is unnecessary and less portable.

#### Optional CC5X Language Server

Phase 1 does not require an LSP.

Phase 2 or 3 can add a lightweight language server if parsing and diagnostics become too expensive for the extension host. It should start with config/register/header intelligence, not a full C parser.

### Data Flow

1. User opens a workspace.
2. Extension scans for `setcc-native.json`.
3. If found, extension runs:
   - `cc5x_setcc_native.py project-show --json`
   - `cc5x_setcc_native.py doctor --json`
4. Extension registers tasks based on manifest editions.
5. User runs `CC5X: Build` or VS Code `Run Task`.
6. Task invokes:
   - `python3 tools/cc5x_setcc_native.py build --project setcc-native.json --edition <edition> --json-diagnostics`
7. Extension parses diagnostics into Problems.
8. Extension refreshes artifact tree.
9. If a Microchip programming task exists, user can program the generated `.hex`.

## Project Files

### `setcc-native.json`

This remains the source of truth for CC5X settings.

Recommended future shape:

```json
{
  "version": 2,
  "projectName": "blink16f1509",
  "device": "PIC16F1509",
  "mainSource": "src/main.c",
  "configSource": "src/main.c",
  "header": {
    "mode": "generated",
    "path": "generated/include/16F1509.H",
    "options": {
      "includeBitNames": true,
      "includeAlternativeNames": true
    }
  },
  "toolchain": {
    "compiler": "CC5X.EXE",
    "runner": "crossover",
    "edition": "extended"
  },
  "editions": {
    "debug": {
      "buildOptions": ["-a", "-L"],
      "config": {
        "FOSC": "INTOSC",
        "WDTE": "OFF"
      }
    },
    "production": {
      "buildOptions": ["-O"],
      "config": {
        "FOSC": "INTOSC",
        "WDTE": "OFF",
        "MCLRE": "ON"
      }
    }
  },
  "outputs": {
    "buildDir": "build/cc5x",
    "hex": "build/cc5x/${edition}/${projectName}.hex"
  }
}
```

### `.vscode/tasks.json`

Generated tasks should be normal VS Code shell/process tasks for CC5X. Example:

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "CC5X: Build production",
      "type": "process",
      "command": "python3",
      "args": [
        "tools/cc5x_setcc_native.py",
        "build",
        "--project",
        "setcc-native.json",
        "--edition",
        "production",
        "--json-diagnostics"
      ],
      "group": "build",
      "problemMatcher": "$cc5x"
    }
  ]
}
```

The extension can also contribute these tasks dynamically through a Task Provider so the repository does not need generated `tasks.json` unless the user wants checked-in tasks.

### Problem Matcher

CC5X output must be normalized in the helper before matching. Recommended JSON diagnostic shape:

```json
{
  "severity": "error",
  "file": "src/main.c",
  "line": 42,
  "column": 1,
  "code": "CC5X123",
  "message": "unknown identifier OPTION_REG"
}
```

The extension should publish Problems directly from JSON. A plain text matcher can be offered for manual tasks, but direct diagnostics are more reliable.

### Optional `.vscode/<project>.mplab.json`

This file should be generated only when useful. It can help the Microchip UI understand:

- project name/configuration
- device
- packs
- file set
- output image path

But CC5X should remain source-of-truth in `setcc-native.json`.

Potential sidecar:

```json
{
  "version": "1.9",
  "configurations": [
    {
      "name": "production",
      "id": "cc5x-production",
      "device": "PIC16F1509",
      "imagePath": "./build/cc5x/production/blink.hex",
      "packs": [
        {
          "name": "PIC16F1xxxx_DFP",
          "vendor": "Microchip",
          "version": "1.29.444"
        }
      ],
      "fileSet": "default",
      "toolchain": "cc5x-external"
    }
  ],
  "propertyGroups": [
    {
      "name": "cc5x-external",
      "type": "toolchain",
      "provider": "cc5x.vscode:cc5x@3.8C",
      "properties": {}
    }
  ],
  "fileSets": [
    {
      "name": "default",
      "files": [
        {
          "include": "**/*",
          "exclude": "**/(build|.vscode|generated)/**/*"
        }
      ]
    }
  ]
}
```

Risk: Microchip extensions may reject unknown toolchain providers in some workflows. Therefore this sidecar should be experimental until tested. If rejected, use `.mplab.json` only for device/file context or skip it entirely.

## Build Integration

### Phase 1 Build

Use CC5X helper tasks:

- Generate/sync config block.
- Generate header if configured.
- Run CC5X through CrossOver/Wine/native Windows path.
- Collect outputs.
- Parse diagnostics.
- Publish VS Code Problems.

The extension task provider should detect each edition in `setcc-native.json` and contribute:

- `CC5X: Build <edition>`
- `CC5X: Clean <edition>`
- `CC5X: Sync Config <edition>`
- `CC5X: Generate Header`
- `CC5X: Validate Project`

### Phase 2 Build

Generate CMake wrapper projects only if it materially improves Microchip interoperability.

Possible model:

- CMake target `cc5x_build_production`.
- Custom command invokes Python helper.
- Output is `.hex`, `.asm`, `.occ`, etc.
- `compile_commands.json` is generated separately for IntelliSense, not treated as a true CC5X compile database.

Risk: Microchip `MPLAB-CMake` assumes Microchip CMake project conventions. A generic CMake wrapper may not be enough to make it first-class in MPLAB UI.

### Phase 3 Build

Investigate Microchip toolchain registration.

Inputs to inspect:

- `microchip.toolchains` extension resources under `resources/opt/*`
- `builds-mplab-opt.json`
- descriptor files
- application finder cache
- `mplab.toolchains.customPaths`

Deliverable:

- proof of concept provider `cc5x`
- one sample `.mplab.json`
- successful `MPLAB CMake: Build` through Microchip UI

Abort criteria:

- if provider format is private, version-fragile, or rejected by Microchip extension runtime
- if implementation requires patching installed extension files

## IntelliSense and Editing

### Immediate Strategy

Use Microsoft C/C++ or Microchip clangd as a best-effort editor engine, but do not treat it as authoritative for CC5X diagnostics.

Generate an IntelliSense support directory:

- `generated/include/<device>.H`
- `generated/vscode/cc5x_intellisense.h`
- `generated/vscode/compile_commands.json`

`cc5x_intellisense.h` should model CC5X-only constructs enough for clang-based parsing:

```c
#ifndef CC5X_INTELLISENSE_H
#define CC5X_INTELLISENSE_H

#define bit unsigned char
#define bank0
#define bank1
#define bank2
#define bank3
#define persistent
#define interrupt

#endif
```

The exact shim must be validated against real CC5X syntax in local examples and refined conservatively.

### Compile Commands

Generate a synthetic `compile_commands.json` using a host compiler command that clangd can parse:

- include generated headers
- include project includes
- force include `cc5x_intellisense.h`
- define `__CC5X__=1`
- define selected device macro

Do not use this for builds. It is editor-only.

### Deeper Language Support

A CC5X language server becomes worthwhile if:

- clangd reports too many false errors
- config/register hover data becomes complex
- CC5X pragmas need proper validation
- artifact-to-source navigation needs indexing

First LSP features:

- diagnostics from `project-validate`
- config symbol completion inside managed config block
- hover for config options
- go-to generated header symbol
- artifact link navigation from `.occ`/`.var`/`.fcs`

## Microchip Debug and Programming Integration

### Programming

The Microchip Debug Adapter supports `MPLAB-DebugAdapter` tasks for `program` and `erase` with direct `device`, `tool`, `serial`, and `image` fields.

Generate tasks like:

```json
{
  "type": "MPLAB-DebugAdapter",
  "label": "CC5X: Program production HEX",
  "action": "program",
  "device": "PIC16F1509",
  "tool": "PICkit 5",
  "image": "${workspaceFolder}/build/cc5x/production/blink.hex"
}
```

This is the best first Microchip integration point because programming a HEX file is less dependent on CC5X debug metadata.

### Debugging

Full source debugging is uncertain until the exact CC5X debug output accepted by Microchip tools is proven.

CC5X may emit `.cod`/`.cof`, but Microchip's current VS Code debug adapter examples and schemas center on modern MPLAB project outputs and program/debug metadata. The plan should test:

- Can MPLAB Debug Adapter launch with only `.hex` for programming?
- Can it debug with `.cof` or `.cod`?
- Can it map source without ELF/DWARF?
- Does simulator support old PIC10/12/16 CC5X outputs?

Debug acceptance criteria:

- breakpoint set in source and hit
- step in/over/out works
- watch simple global
- inspect registers or I/O view
- program counter maps to source or assembly

If this fails, ship programming support first and document debug as limited.

## Device Pack and MCC Interaction

CC5X extension should use Microchip packs directly through the existing Python library.

MCC interaction is limited:

- MCC can generate source for supported Microchip workflows.
- CC5X cannot assume MCC output is CC5X-compatible.
- If user imports MCC-generated code, extension should treat it as source files and build only after CC5X syntax compatibility is addressed.

Useful integration:

- read selected device and pack version from `.mplab.json`
- use same local `.mchp_packs/Microchip` cache
- generate links to datasheet/product page through Microchip UI command where possible

Do not attempt to make MCC generate CC5X-native code in phase 1.

## Extension Manifest Sketch

```json
{
  "name": "cc5x-vscode",
  "displayName": "CC5X Tools",
  "publisher": "local",
  "engines": {
    "vscode": "^1.90.0"
  },
  "extensionDependencies": [
    "microchip.mplab-extensions-core"
  ],
  "activationEvents": [
    "workspaceContains:setcc-native.json",
    "onCommand:cc5x.doctor",
    "onCommand:cc5x.build"
  ],
  "contributes": {
    "commands": [
      {
        "command": "cc5x.doctor",
        "title": "Doctor",
        "category": "CC5X"
      },
      {
        "command": "cc5x.build",
        "title": "Build",
        "category": "CC5X"
      }
    ],
    "taskDefinitions": [
      {
        "type": "CC5X",
        "required": ["action", "project", "edition"],
        "properties": {
          "action": {
            "type": "string",
            "enum": ["build", "clean", "sync-config", "generate-header", "validate"]
          },
          "project": {
            "type": "string"
          },
          "edition": {
            "type": "string"
          }
        }
      }
    ],
    "configuration": {
      "type": "object",
      "title": "CC5X",
      "properties": {
        "cc5x.pythonPath": {
          "type": "string",
          "default": "python3"
        },
        "cc5x.helperPath": {
          "type": "string",
          "default": "${workspaceFolder}/tools/cc5x_setcc_native.py"
        },
        "cc5x.autoDiscoverProjects": {
          "type": "boolean",
          "default": true
        },
        "cc5x.generateIntelliSenseFiles": {
          "type": "boolean",
          "default": true
        },
        "cc5x.enableMplabProgramTasks": {
          "type": "boolean",
          "default": true
        }
      }
    }
  }
}
```

## Back-End CLI Contract

Every extension-facing command should support:

- `--json`
- deterministic output schema
- nonzero exit code on failure
- clear `error.kind`
- clear `error.message`
- optional `error.details`

Example:

```json
{
  "ok": false,
  "error": {
    "kind": "missing_compiler",
    "message": "CC5X.EXE was not found in the configured CrossOver bottle",
    "details": {
      "searched": [
        "/home/gary/.cxoffice/...",
        "/home/gary/apps/cc5x_paid/CC5X/CC5X.EXE"
      ]
    }
  }
}
```

Build result:

```json
{
  "ok": true,
  "project": "blink16f1509",
  "edition": "production",
  "device": "PIC16F1509",
  "command": ["crossover", "run", "CC5X.EXE", "-p16F1509", "src/main.c"],
  "artifacts": {
    "hex": "build/cc5x/production/blink.hex",
    "asm": "build/cc5x/production/blink.asm",
    "occ": "build/cc5x/production/blink.occ"
  },
  "diagnostics": []
}
```

## Implementation Phases

### Phase 0: Confirm Tool Behavior

Tasks:

- Run `python3 tools/cc5x_setcc_native.py doctor`.
- Run a known compiler-validated generated-header build.
- Confirm current CrossOver/Wine invocation.
- Confirm Microchip `MPLAB-DebugAdapter` can program a known HEX from a task.
- Confirm whether `.mplab.json` with unknown toolchain provider is ignored, rejected, or partially accepted.

Acceptance:

- One sample CC5X project builds from terminal.
- One sample HEX can be programmed through Microchip task, or failure mode is documented.
- Unknown-provider behavior is known.

### Phase 1: VS Code Companion Extension MVP

Tasks:

- Scaffold TypeScript VS Code extension.
- Implement project discovery for `setcc-native.json`.
- Implement command runner wrapper for Python helper.
- Add `CC5X: Doctor`.
- Add `CC5X: Build`.
- Add task provider for manifest editions.
- Add output channel.
- Add diagnostics collection from JSON output.
- Add artifact tree view.

Acceptance:

- Opening this repo activates the extension.
- `CC5X: Doctor` reports local readiness.
- `Ctrl+Shift+B` can run `CC5X: Build production`.
- Errors appear in VS Code Problems.
- Artifact view refreshes after build.

### Phase 2: Project Authoring and Config UX

Tasks:

- Add create-project wizard.
- Add device picker backed by `list-devices --json`.
- Add config editor webview or quick-pick flow.
- Add `sync-config` CodeLens.
- Add generated header command.
- Add manifest validation diagnostics.

Acceptance:

- User can create a new PIC16F project without editing JSON by hand.
- User can select legal config values from pack metadata.
- Managed config block updates source deterministically.
- Generated header compiles for at least current validated devices.

### Phase 3: IntelliSense Support

Tasks:

- Generate `cc5x_intellisense.h`.
- Generate editor-only `compile_commands.json`.
- Add settings helper for Microsoft C/C++ and/or Microchip clangd.
- Add command to refresh IntelliSense files.
- Add false-positive suppression guidance.

Acceptance:

- Open `main.c` has useful symbols and navigation.
- Generated CC5X header is visible to IntelliSense.
- CC5X-specific syntax causes tolerable editor noise.
- Real CC5X build remains authoritative.

### Phase 4: Microchip Programming Integration

Tasks:

- Generate `MPLAB-DebugAdapter` program/erase tasks.
- Add tool picker using Microchip command if callable, otherwise user setting.
- Add optional `.vscode/launch.json` program-only profile.
- Validate PICkit/PKOB/Snap path.

Acceptance:

- Build task can be followed by program task.
- Device, tool, image path are populated from manifest.
- Missing tool/serial gives clear guidance.

### Phase 5: Optional MPLAB Project Sidecar

Tasks:

- Generate `.vscode/<project>.mplab.json`.
- Include device, packs, file set, image path.
- Test Microchip Project View behavior.
- Test CMake runner behavior.
- Decide whether sidecar should be default, opt-in, or abandoned.

Acceptance:

- Microchip Project View provides useful device/project context without breaking CC5X workflow.
- If unknown provider causes trouble, extension disables sidecar generation by default.

### Phase 6: Experimental Toolchain Provider

Status after local research: blocked for normal implementation.

The Microchip descriptor/build schema can describe a CC5X wrapper in principle, but the installed `microchip.toolchains` custom-path path only scans a hardcoded compiler ID list. `cc5x` is not in that list, so `MPLAB: Add Toolchain` will not discover a true CC5X descriptor by itself. See [MICROCHIP_TOOLCHAIN_REGISTRATION_FEASIBILITY.md](MICROCHIP_TOOLCHAIN_REGISTRATION_FEASIBILITY.md).

Tasks:

- Inspect `microchip.toolchains` provider descriptors.
- Build a local CC5X descriptor if feasible.
- Register via supported custom path/settings if possible.
- Avoid patching extension files.
- Test `MPLAB CMake: Build`.

Acceptance:

- Either a working first-class toolchain provider proof exists, or the plan records why it is not stable enough.

### Phase 7: Debug Research

Tasks:

- Generate CC5X `.cod`/`.cof` outputs.
- Test Microchip debug adapter with simulator and hardware.
- Test breakpoints, source stepping, register view, watch view.
- Test source-file mapping.
- Determine whether conversion to ELF/DWARF is realistic or not.

Acceptance:

- Debug capability matrix:
  - program only
  - assembly/source debug
  - simulator
  - hardware
  - unsupported cases

## Testing Strategy

Back end:

- Unit tests for JSON command output.
- Golden tests for generated headers.
- Golden tests for generated config blocks.
- CLI smoke tests for each extension-facing command.
- Real compiler validation for selected devices.

Extension:

- Unit tests for manifest parsing.
- Unit tests for diagnostic conversion.
- Unit tests for task generation.
- VS Code extension host integration test:
  - open fixture workspace
  - discover manifest
  - run doctor
  - provide tasks
  - simulate build output

System:

- Fixture projects:
  - `PIC10F200`
  - `PIC12F1840`
  - `PIC16F1509`
  - `PIC16F15313`
- Verify:
  - build success
  - artifact detection
  - diagnostics on intentional error
  - config editor legal values
  - program task JSON generation

## Risks and Mitigations

### Risk: Microchip Toolchain API Is Private

Mitigation:

- Treat first-class toolchain registration as experimental.
- Use VS Code tasks and Microchip programming tasks as stable integration surfaces.

### Risk: CC5X Diagnostics Are Hard To Parse

Mitigation:

- Parse in Python helper where compiler-specific behavior already belongs.
- Emit structured JSON for extension.
- Keep text problem matcher as fallback only.

### Risk: IntelliSense Misrepresents CC5X

Mitigation:

- Mark IntelliSense as editor-only.
- Force real CC5X build as authority.
- Keep shim small and explicit.

### Risk: Debugging Is Limited

Mitigation:

- Ship programming support first.
- Build a debug evidence matrix.
- Avoid promising ELF/DWARF-style debug until proven.

### Risk: Pack Metadata Drift

Mitigation:

- Use deterministic pack selection.
- Show selected pack in UI.
- Include pack version in generated files.
- Keep generated output reproducible.

## Deliverables

Minimum useful release:

- VS Code extension source under `vscode/cc5x-vscode/`
- `CC5X: Doctor`
- `CC5X: Build`
- task provider
- diagnostics
- artifact view
- generated `.vscode/tasks.json` support
- documentation

Professional release:

- project wizard
- device picker
- config editor
- header generation UI
- IntelliSense file generation
- Microchip program task generation
- tested sample projects
- extension host tests

Research release:

- `.mplab.json` sidecar generator
- Microchip Project View compatibility report
- toolchain-provider feasibility report
- debug capability matrix

## Source References

- Microchip MPLAB Extensions overview: https://developerhelp.microchip.com/xwiki/bin/view/software-tools/ides/extensions/
- Microchip MPLAB extension pack marketplace page: https://marketplace.visualstudio.com/items?itemName=Microchip.mplab-extension-pack
- Microchip CMake Runner marketplace page: https://marketplace.visualstudio.com/items?itemName=Microchip.runcmake
- Microchip Debug Adapter marketplace page: https://marketplace.visualstudio.com/items?itemName=Microchip.mplab-core-da
- Microchip migration guide, DS40002665B: https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/SupportingCollateral/MigrationGuide-MPLAB-X-IDE-MPLABTools-VSCode-DS40002665.pdf
- VS Code Task Provider API: https://code.visualstudio.com/api/extension-guides/task-provider
- VS Code C/C++ settings reference: https://code.visualstudio.com/docs/cpp/customize-cpp-settings
- VS Code C/C++ overview: https://code.visualstudio.com/docs/languages/cpp
- VS Code Language Server Extension Guide: https://code.visualstudio.com/api/language-extensions/language-server-extension-guide
