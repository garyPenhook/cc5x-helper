# CC5X Native SETCC Reimplementation Plan

## Purpose

This document is the implementation plan Codex should follow to build a professional Linux-native replacement for `SETCC.EXE`.

The replacement must prefer modern Microchip PIC DFP archives found on this machine in `/home/gary/apps/*.atpack`, and only use legacy MPLAB-style `.ini/.cfgdata` discovery as fallback.

The immediate goal is not to clone the old GUI. The goal is to replace the useful engineering workflows behind it with a scriptable, testable, source-controlled toolchain.

## Ground Truth Established

- `SETCC.EXE` is a 32-bit Windows VCL GUI binary with no local source.
- The newer bundled binary is `cc5x_paid/CC5X/setcc.exe`, not the older top-level `SETCC.EXE`.
- Reverse engineering identified these major internal responsibilities:
  - device metadata discovery
  - header generation
  - config block generation and source update
  - compiler launch orchestration
- The machine has modern PIC DFP archives in `/home/gary/apps`:
  - `Microchip.PIC16Fxxx_DFP.1.7.162.atpack`
  - `Microchip.PIC16F1xxxx_DFP.1.29.444.atpack`
  - `Microchip.PIC12-16F1xxx_DFP.1.8.254.atpack`
- Those archives contain:
  - `edc/*.PIC`
  - `xc8/pic/dat/ini/*.ini`
  - `xc8/pic/dat/cfgdata/*.cfgdata`
  - `.pdsc`
- The shipped CC5X headers already expose dynamic config symbols via `#pragma config /<reg> ...` entries.
- Shipped CC5X headers are useful validation samples, but they are not authoritative coverage for newer PIC devices.
- `getting_started_C.pdf` (`TB3261`) should be treated as a detailed reference for modern Microchip PIC header naming, multibyte register naming, legacy-peripheral exceptions, and the requirement to specify configuration bits explicitly.

## Product Direction

Build a CLI-first replacement with a clean internal library.

The recommended structure is:

- `tools/cc5x_setcc_native.py`
  - thin CLI entrypoint
- `tools/cc5x_setcc_native/`
  - `packs.py`
  - `legacy.py`
  - `picmeta.py`
  - `headergen.py`
  - `configgen.py`
  - `build.py`
  - `project.py`
  - `cli.py`
  - `models.py`

The current single-file script is an acceptable bootstrap. It should be refactored into modules once header generation from packs begins.

## Scope

### In Scope

- Read PIC metadata directly from `.atpack` archives without manual extraction.
- Read legacy MPLAB-style metadata from unpacked trees when needed.
- Generate CC5X-compatible device headers for supported PIC families.
- Generate and update managed config blocks in source files.
- Launch CC5X builds from Linux in a reproducible way.
- Support project state in an open text format.
- Add tests for parsing, generation, and source update logic.

### Out Of Scope For Initial Delivery

- Recreating the original Windows GUI.
- Binary patching or modifying `SETCC.EXE`.
- Importing the opaque `setcc.pxk` format unless its format is later decoded.
- Full MPLAB integration beyond pack and metadata consumption.

## Architecture Decisions

### Metadata Source Priority

Use this order:

1. `.atpack` PIC DFP archives in `/home/gary/apps`
2. unpacked pack trees such as `~/.mchp_packs/Microchip`
3. legacy MPLAB X trees containing `.ini` and `.cfgdata`
4. shipped CC5X headers only when generation is not required

Treat shipped BKND headers as compatibility references, not as a completeness requirement. The tool must remain useful for devices that exist in current Microchip packs but have no bundled CC5X header.

### Primary Device Model

Normalize all source formats into one internal representation:

- device name
- family / core type
- module family
- registers
- bitfields
- aliases
- canonical names versus compatibility aliases
- config registers
- config symbols and allowed values
- ID locations
- memory limits relevant to CC5X header generation
- feature flags needed to reproduce CC5X header layout

Do not let header generation depend directly on raw `.PIC` or `.cfgdata` line formats.

TB3261 implies that the model should preserve enough naming structure to distinguish:

- canonical long-form names
- shorter compatibility names
- legacy-peripheral exceptions

### Output Philosophy

Generated artifacts should be deterministic:

- stable sort order
- stable whitespace
- stable comment formatting
- stable alias emission rules

This matters for version control and regression testing.

Generated artifacts should also respect two different goals:

1. modern-pack semantic fidelity
2. CC5X compatibility ergonomics

Those goals overlap, but they are not identical. The compatibility layer should be explicit and testable.

## Phased Plan

## Phase 1: Stabilize The Existing Bootstrap

### Objectives

- Clean up the current single-file prototype.
- Preserve the already working features.
- Make probe output trustworthy.

### Tasks

- Refactor pack discovery helpers to support:
  - `.atpack`
  - unpacked packs
  - legacy MPLAB roots
- Make pack selection deterministic when multiple archives contain the same device.
  - prefer the newest semantic version
- Expand `probe` output to show:
  - chosen source type
  - chosen pack/archive
  - available companion files
- Add CLI tests for:
  - `probe`
  - `list-config`
  - `render-config`
  - `sync-config`

### Acceptance Criteria

- `probe --device PIC16F1509` resolves the pack in `/home/gary/apps`.
- `probe --device PIC16F15313` resolves the pack in `/home/gary/apps`.
- `probe` output clearly distinguishes `.atpack` vs unpacked pack vs legacy MPLAB source.
- Existing script commands still pass basic smoke tests.

## Phase 2: Build A Proper Pack Reader

### Objectives

- Read PIC metadata directly from `.atpack`.
- Stop treating `.atpack` as just a locator.

### Tasks

- Implement `packs.py`:
  - enumerate available archives
  - parse semantic versions from filenames
  - open zip members lazily
  - expose file lookup by logical role:
    - `.PIC`
    - `.ini`
    - `.cfgdata`
    - `.pdsc`
- Add a `PackDeviceSource` model that returns text or bytes from archive members.
- Build robust lookup logic for devices whose names appear in both plain and accessory-prefixed `.PIC` forms.

### Acceptance Criteria

- The tool can read raw member contents from:
  - `edc/PIC16F1509.PIC`
  - `xc8/pic/dat/ini/16f1509.ini`
  - `xc8/pic/dat/cfgdata/16f1509.cfgdata`
- The chosen pack is deterministic when multiple PIC packs match a device.

## Phase 3: Decode The Metadata Into One Internal Model

### Objectives

- Convert raw pack metadata into a single normalized device description.

### Tasks

- Reverse engineer the practical fields needed from:
  - `.PIC`
  - `.ini`
  - `.cfgdata`
- Compare generated CC5X headers with pack metadata for a few known devices:
  - `PIC16F1509`
  - `PIC16F15313`
  - one PIC18 device if present in packs or shipped headers
- Build `picmeta.py` with parsers and normalized models.
- Capture:
  - register addresses
  - register aliases
  - bit names and alternate names
  - module-family classification where needed for naming policy
  - config symbol names
  - config register numbers
  - value masks
  - symbolic states
  - comments/descriptions

### Acceptance Criteria

- The normalized model contains enough information to regenerate at least config symbol sections accurately.
- Parsed metadata for `PIC16F1509` and `PIC16F15313` matches shipped CC5X header semantics where those headers exist.
- The model is still sufficient to drive generation for newer devices that have no shipped BKND header.
- The model can distinguish modern Microchip canonical names from compatibility aliases when the data implies both.

## Phase 4: Implement Header Generation

### Objectives

- Generate CC5X-style header files from the normalized model.

### Tasks

- Implement `headergen.py`.
- Reproduce these header sections:
  - register definitions
  - bit definitions
  - optional alias definitions
  - dynamic config section
- Support the user-visible options documented by SETCC where practical:
  - include all alternative register names
  - include bit names
  - include alternative bit names
  - include enumerated bit names
  - bit naming format controls
- Add explicit policy hooks for legacy-peripheral families called out by TB3261:
  - EUSART
  - MSSP
- Define a minimal first-pass subset if all options cannot ship at once.
- Add a formatting layer so generated headers are diff-stable.

### Acceptance Criteria

- For selected test devices, generated config sections match the shipped CC5X headers semantically.
- Register and bit symbol emission is stable and repeatable.
- The generator writes a complete CC5X-usable header file for at least one supported PIC16F1 device.
- The generator does not require a shipped CC5X header to support a device.
- The generator has explicit, tested rules for modern canonical names versus compatibility aliases in at least EUSART and MSSP cases.

## Phase 5: Implement Config Generation Professionally

### Objectives

- Replace SETCC’s source-config workflow with a deterministic source updater.

### Tasks

- Keep the existing managed block format.
- Add support for:
  - preserving surrounding file content
  - replacing only the managed block
  - generating defaults from metadata
  - validating requested symbol/value pairs against the header or normalized metadata
  - preferring explicit full configuration output for reproducibility
- Support both:
  - symbol-only config from generated headers
  - config generation directly from normalized metadata
- Add a machine-readable export mode such as JSON for IDE integration.

### Acceptance Criteria

- `sync-config` is idempotent.
- Invalid symbol names or states fail with precise diagnostics.
- Config blocks generated from metadata match CC5X expectations.
- The default project workflow can emit explicit full-device configuration in line with TB3261 guidance.

## Phase 6: Add Project Files And Build Orchestration

### Objectives

- Replace the hidden `setcc.pxk` state with an open project format.

### Tasks

- Define `setcc-native.json`:
  - device
  - metadata source
  - compiler path
  - header path
  - config source path
  - source file
  - build options
  - named editions such as `production` and `debug`
- Implement:
  - `project init`
  - `project validate`
  - `build --project`
  - `config sync --project --edition`
- Decide whether to support TOML instead of JSON.
  - JSON is simpler.
  - TOML is more readable.

### Acceptance Criteria

- A project can be built without any GUI interaction.
- Named editions produce different config blocks and build flags deterministically.

### Status

- First-pass implementation completed:
  - `project-init`
  - `project-validate`
  - `sync-config --project --edition`
  - `build --project --edition`
- Current manifest is `setcc-native.json`.
- Current scope covers the useful persisted state from `setcc.pxk`, not every GUI preference.

## Phase 7: Verification Against Real Devices

### Objectives

- Prove the replacement is correct enough to trust.

### Tasks

- For each selected device:
  - generate a header from the modern pack
  - compare it against the shipped CC5X header when one exists
  - compile a minimal C file using the generated header
  - emit `.asm`, `.var`, and `.occ`
- Record mismatches:
  - harmless formatting drift
  - missing aliases
  - incorrect config symbols
  - register address mismatch
- Fix semantic mismatches first.

### Suggested Validation Set

- `PIC16F1509`
- `PIC16F15313`
- `PIC16F1789` or similar if available
- one legacy PIC16 from `PIC16Fxxx_DFP`

### Acceptance Criteria

- Generated headers compile cleanly with CC5X for the validation set.
- Config blocks generated by the tool compile and produce expected config words.

## Phase 8: Finish The User Experience

### Objectives

- Make the tool usable without needing to read its source.

### Tasks

- Improve help text and command naming.
- Add `--json` output where useful.
- Add a `doctor` command:
  - locate CC5X
  - locate pack archives
  - report missing dependencies such as `wine`
- Add a `list-devices` command driven by pack metadata.
- Write user-facing docs and examples.

### Acceptance Criteria

- A new user can locate a device, generate a header, sync config, and build a project from the CLI alone.

## Test Strategy

### Unit Tests

- device name normalization
- pack version selection
- archive member lookup
- config line parsing
- source block replacement
- project file validation

### Fixture Strategy

- Do not rely only on the large real pack archives in tests.
- Create small test fixtures containing:
  - one `.PIC`
  - one `.ini`
  - one `.cfgdata`
  - one `.pdsc`
- Keep a small number of integration tests that use the real local archives when available.

### Integration Tests

- `probe` against local `.atpack` archives
- header generation for a known device
- config sync into a temporary C file
- build command composition with `--dry-run`

## Implementation Order For Codex

Codex should follow this order:

1. Stabilize the current script and add tests.
2. Add direct `.atpack` archive reading as a first-class API.
3. Build the normalized device model.
4. Generate config metadata from pack contents.
5. Generate full CC5X headers.
6. Add project-file support.
7. Validate against shipped headers and real CC5X compilation.
8. Improve CLI UX and docs.

## Immediate Next Task Recommendation

The next task should be:

`Implement a pack reader that opens the .atpack archives in /home/gary/apps, selects the newest matching PIC DFP for a requested device, and exposes .PIC, .ini, .cfgdata, and .pdsc members through one clean API with tests.`

That is the correct professional next step because it creates the foundation for the rest of the reimplementation.

## Risks

- `.PIC` format details may be more complex than the current plan assumes.
- Different pack families may encode metadata slightly differently.
- CC5X header generation may depend on subtle name-selection rules not obvious from pack files alone.
- Some SETCC options may have behavior that is easiest to infer by diffing generated headers across devices rather than by parsing documentation alone.

## Risk Mitigations

- Validate each parser stage against shipped CC5X headers.
- Validate naming-policy decisions against both shipped BKND headers and TB3261 naming guidance.
- Keep a normalized internal model instead of coupling generators to raw file formats.
- Add golden-file tests for generated headers and config sections.
- Keep CLI surface small until the metadata pipeline is solid.

## Deliverables

Minimum professional deliverable:

- reliable `probe`
- pack reader
- normalized metadata layer
- config block generator
- header generator for selected PIC16F1 devices
- open project format
- tests
- usage docs

Target stretch deliverable:

- broad PIC family coverage
- `list-devices`
- `doctor`
- project editions and build profiles
