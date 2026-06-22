# CC5X → VS Code + PICkit 4/5 Integration — TODO

Tracking checklist for integrating the CC5X compiler into VS Code with PICkit 4
(and above) programming support. Derived from:

- `CC5X_VSCODE_MICROCHIP_EXTENSION_DESIGN.md`
- `MICROCHIP_TOOLCHAIN_REGISTRATION_FEASIBILITY.md`
- `MICROCHIP_PATCHING_PRIVATE_INTEGRATION_RESEARCH.md`

Boundary principle: **CC5X produces a `.hex`; the programmer consumes a `.hex`.**
The two halves (build vs. flash) are decoupled and can progress independently.

### Confirmed local toolchain (2026-06-22)

- MPLAB X **v6.30** installed at `/opt/microchip/mplabx/v6.30` (Linux).
- Programming CLI: `/opt/microchip/mplabx/v6.30/mplab_platform/mplab_ipe/ipecmd.sh`
  (also `ipecmd.jar`). **IPECMD is Microchip's cross-platform production programming CLI,
  officially supported on Linux** — verified via Microchip MCP docs.
- Debug CLI: `/opt/microchip/mplabx/v6.30/mplab_platform/bin/mdb.sh` (MDB debugger).
- XC8 present under `/opt/microchip/xc8`; Microchip VS Code extensions installed.
- PICkit 4 programs PIC10/12/16 over ICSP (PGC/PGD/MCLR), runs on Linux — verified via MCP.

**IPECMD invocation shape** (verified from local `docs/Readme for IPECMD.htm`):
`ipecmd.sh -P<device> -TP<tool> -F<hexfile> -M`
  - `-P<device>` e.g. `-P16F1509`
  - `-TP<tool>` tool-select code (confirmed): `PK4`=PICkit 4, `PPK5`=PICkit 5, `SNAP`=Snap,
    `PPK3`=PICkit 3, `ICD4`/`ICD5`/`ICD3`, `PKOB`/`PKOB4`, `J32`, `PKBASIC`
  - `-F<hexfile>` path to the CC5X-produced HEX
  - `-M` program the device (read erase/verify/`-OL` flags from the IPECMD readme)

This means programming can be a **plain VS Code task that shells out to `ipecmd.sh`** —
no dependency on the MPLAB-DebugAdapter extension and fully Linux-native.

---

## AUDIT FINDINGS — bugs to fix (from 2026-06-22 multi-angle review)

These were defects in work completed this session. Ordered by severity. Correctness first.
**All resolved 2026-06-22** (fixes verified: 65 Python tests pass, extension compiles clean
under strict tsc, `doctor` reports ready/303 devices). A second multi-angle review of the fixes
surfaced one further real bug (JSONC trailing-comma-before-comment), now also fixed + tested.

### Correctness (active bugs — fix before relying on the affected feature)

- [x] **A1 🔴 GUI build path not updated.** **Fixed:** extracted `build_options_for_project()`
      (shared by `cmd_build` and the GUI `_run_build`) so both make the same `-p`/`#pragma chip`
      decision; added `merged_device_list()` (atpacks + installed packs) used by both the CLI
      `list-devices` and GUI `show_devices`. GUI family list now derives from
      `CC5X_DEVICE_PREFIXES`. Regression tests in `tests/test_build.py::BuildOptionsForProjectTests`.

- [x] **A2 🔴 Extension "Program Device" reports failure on success.** **Fixed:** `cmd_program`
      now captures IPECMD stdout/stderr into the JSON payload when `--json` (pure JSON on stdout);
      human mode still streams. Extension echoes captured output to the channel. Test:
      `ProgramJsonCaptureTests`.

- [x] **A3 🔴 `vscode-tasks` merge breaks on real (JSONC) `tasks.json`.** **Fixed:** added
      `strip_jsonc()` (comments + trailing commas, string-aware, two-pass) before `json.loads`;
      `--force` now backs up an unparseable file to `.bak` instead of clobbering. Tests:
      `StripJsoncTests`, `VscodeTasksMergeTests`. (Second-review follow-up: fixed a
      trailing-comma-immediately-before-a-comment case that the first pass missed.)

- [x] **A4 🟠 Build no longer guarantees it targets `project.device`.** **Fixed:**
      `header_chip_name()` + a mismatch check in `build_options_for_project()` refuse the build
      when a header's `#pragma chip` ≠ the manifest device. Test: `test_rejects_chip_device_mismatch`.

- [x] **A5 🟠 Duplicate "CC5X: Build <edition>" tasks.** **Fixed:** generated `tasks.json` is the
      single source of runnable tasks; the extension `Cc5xTaskProvider.provideTasks()` returns `[]`
      (keeps `resolveTask` for hand-authored `type: cc5x` tasks). `cc5x.build` command still covers
      zero-config one-off builds.

### Latent / platform (fix before multi-version / multi-root / non-default setups)

- [x] **A6 🟠 lexical MPLAB X version sort.** **Fixed:** numeric `parse_version()` ordering; both
      `discover_ipecmd` and `discover_pack_roots` now share `mplabx_version_dirs()` (newest-first).
      Test: `test_discover_pack_roots_orders_mplabx_versions_numerically`.

- [x] **A7 🟡 supplied-header case sensitivity.** **Fixed:** `_resolve_supplied_header()` resolves
      `<short>.H` case-insensitively. Test: `ResolveSuppliedHeaderTests`.

- [x] **A8 🟡 Extension multi-root + watcher gaps.** **Fixed:** `workspaceRoot()` locates the
      folder that contains the manifest; `manifestWatchPattern()` handles an absolute
      `cc5x.manifest`; watcher gained `onDidDelete` clearing `currentProject`.

### Quality / efficiency (hardening)

- [x] **A9 🟡 Version-key parsing duplicated 3×.** **Fixed:** one shared `parse_version()` (handles
      a leading `v`, swallows non-numeric to `()`); removed `_version_key_from_name` and the
      fabricated-`.atpack` reparse. Tests: `ParseVersionTests`.

- [x] **A10 🟡 `doctor` heaviness + artifacts UI-thread walk.** **Fixed:** `environment_report`
      computes pack roots + validated set once; `artifacts.ts` walk is async (`fs.promises`) and
      cached with a generation guard so a slow scan racing a refresh cannot serve stale results.

- [x] **A-minor 🟡 Diagnostic regex mismatch.** **Fixed:** `diagnostics.ts` now uses `:\s+`,
      matching the contributed `$cc5x` problem matcher.

- [x] **A11 🟡 (found by second review) `find_device_in_unpacked_packs` missing `exists()` guard**
      on an explicit `roots=` list (its sibling `list_devices_in_unpacked_packs` has one).
      **Fixed:** added the guard so a removed/non-existent root yields empty instead of crashing.

---

## Phase 0 — De-risk (do first, ~½ day)

- [x] Run `python3 tools/cc5x_setcc_native.py doctor` and record current readiness.
      → exit 0. `ready: no` **only** because pack discovery scans `/home/gary/apps` +
      `/home/gary/Downloads` and finds 0 DFP packs; runner + compiler + CrossOver bottle all
      present; 12 validated devices. (Pack discovery should also scan
      `/opt/microchip/mplabx/v6.30/packs` — see follow-up below.)
- [x] Build one compiler-validated device end-to-end from the terminal (PIC16F1509).
      → **PASS.** CC5X 3.8C ran via CrossOver/Wine and produced `main.hex` **byte-for-byte
      identical** to the validated reference `16f1509_gen.hex`. Reproducible.
- [x] Confirm the CrossOver/Wine/native invocation path for `CC5X.EXE`.
      → `/home/gary/apps/cc5x-run.sh` (wine `--bottle CC5X` → `C:\Program Files\bknd\CC5X\CC5X.EXE`).

### Phase 0 findings (2026-06-22)

- **Build invocation:** generated headers carry `#pragma chip PIC16F1509, ...`, so the build
  must **NOT** also pass `-p<device>` — doing so triggers `Error: Duplicate chip definition`.
  Device selection comes from the header in this workflow. The `build` helper's
  `--project` mode currently appends `-p<dev>`, which will conflict with `#pragma chip`
  headers — needs reconciling before wiring the extension build task.
- **Header bug found:** `validation/generated/16F1509/16F1509.H` (1339-line variant) is
  **broken** — duplicate `D1N` bit declaration → `Error: No ';' found` at line 831. The
  known-good header is `validation/generated/16F1509/generated/16F1509.H` (1152 lines), which
  is what produced the validated HEX. Two divergent header variants for the same device should
  be reconciled / the broken one removed or regenerated.
- **argparse gotcha:** pass compiler flags as `--option=-p16F1509` (the `=` form), not
  `--option -p16F1509`, or argparse treats the value as a flag.

### Phase 0 follow-ups (new TODOs)

- [x] Make pack discovery **system-wide** so `doctor` reports `ready: yes` and pack-driven
      device metadata works.
      → `discover_pack_roots()` now searches, in order: `$CC5X_PACK_ROOTS`/`$MPLABX_PACKS`
      env override, `~/.mchp_packs/Microchip`, **every** installed MPLAB X version found under
      the standard Linux/macOS/Windows install bases (`/opt/microchip/mplabx/*/packs/Microchip`,
      etc.), then legacy `~/apps`. New `list_devices_in_unpacked_packs()` lists devices from
      unpacked trees; `doctor` + `list-devices` now merge archive + installed packs.
      Verified: `doctor` → `ready: yes`, 303 CC5X devices discovered from MPLAB X v6.30;
      `describe-device PIC16F1509` resolves full metadata from the installed packs.
      Covered by new tests in `tests/test_packs.py` (28 tests pass).
- [x] Reconcile `-p<device>` vs `#pragma chip` so `build --project` doesn't emit a duplicate
      chip definition for generated-header projects.
      → `build --project` now adds `-p<device>` only when the resolved header lacks a
      `#pragma chip` (new `header_defines_chip()` in `cc5x_setcc_native.py`). Verified: header
      with `#pragma chip` builds clean (HEX matches reference), header without it gets `-p`.
      Covered by `tests/test_build.py` (24 tests pass).
      ✅ **RESOLVED (A1, A4):** both CLI and GUI now share `build_options_for_project()`, and a
      header `#pragma chip` that disagrees with `project.device` aborts the build.
- [~] Broken 1339-line `16F1509.H`: **do not hand-edit header files** (per project rule). The
      build already resolves the canonical generated header (the 1152-line variant under
      `generated/`); if the broken variant must be replaced, **regenerate** it via
      `render-pack-header` rather than editing it by hand. No action taken on the file itself.
- [ ] **Prove a known HEX flashes to the PICkit 4** from the terminal. With the helper:
      `python3 tools/cc5x_setcc_native.py program --device PIC16F1509 --hex <hex> --tool PK4`
      (add `--dry-run` first to preview; emits `ipecmd.sh -P16F1509 -TPPK4 -F<hex> -M`).
      This single test de-risks the entire programming half. **Needs hardware connected.**
- [ ] Read `docs/Readme for IPECMD.htm` for exact erase/verify/voltage/speed flags.
- [ ] Confirm intended target devices are in MPLAB X v6.30 device support (packs present).
- [ ] (Optional) Note unknown-`.mplab.json`-provider behavior if pursuing the sidecar later.

**Exit:** one project builds from terminal; one HEX programs via `ipecmd.sh` + PICkit 4
(or failure documented).

---

## Phase 1 — Python CLI JSON contract (foundation)

The bulk of the *reliable* work lives here. Do not reimplement this logic in TypeScript.

- [ ] Add `--json` output + deterministic schema to `doctor`.
- [ ] Add `build --json-diagnostics` (normalize CC5X output to structured diagnostics).
- [ ] Add `artifacts --json` (`.hex/.asm/.occ/.var/.fcs/.cpr/.cod/.cof`).
- [ ] Add `project-show --json` and `list-devices --json`.
- [ ] Standardize exit codes (nonzero on failure) and `error.kind` / `error.message` / `error.details`.
- [ ] Add `--workspace-root` and default `--project` discovery for `setcc-native.json`.
- [ ] Unit tests for each JSON command output.
- [ ] Golden tests for generated headers and config blocks.

**Exit:** every extension-facing command emits stable JSON with stable exit codes.

---

## Phase 2 — VS Code companion extension MVP (`cc5x-vscode`)

- [x] Scaffold TypeScript extension at `vscode/cc5x-vscode/` (compiles clean under strict tsc;
      Node 24 / npm 11; `@types/vscode ^1.84`).
- [x] `package.json` manifest: commands, `cc5x` task type, settings (`cc5x.pythonPath`,
      `cc5x.helperPath`, `cc5x.manifest`, `cc5x.programmerTool`), artifact view, `$cc5x`
      problem matcher.
- [x] Activation on `workspaceContains:setcc-native.json`.
- [x] Project discovery via `project-show --json` (manifest watcher reloads on change).
- [x] Command runner that shells out to the Python helper (`spawn`, no shell quoting;
      `runHelper` streams to the output channel, `runHelperJson` parses `--json`).
- [x] `CC5X: Doctor` command (parses `doctor --json`, drives a status-bar indicator).
- [x] `CC5X: Build` command + `Cc5xTaskProvider` contributing one `Build <edition>` task per
      edition (first = default build; `$cc5x` matcher).
- [x] Output channel ("CC5X").
- [x] Diagnostics: parse CC5X text (`(Error|Warning) <file> <line>: <msg>`) → Problems panel.
      Regex verified against real CC5X output and against non-diagnostic lines that must NOT
      match (`Warnings (level 1-3): …`, `Error options:`).
- [x] Artifact tree view (`.hex/.asm/.occ/.var/.fcs/.cpr/.cod/.cof`) with manual refresh.
- [x] Bonus: `CC5X: Program Device` (modal confirm before writing hardware) and
      `CC5X: Generate VS Code Tasks` commands wired to the helper.

Modules: `src/extension.ts` (wiring), `src/helper.ts` (runner + settings + discovery),
`src/diagnostics.ts` (CC5X text → Diagnostics), `src/tasks.ts` (task provider),
`src/artifacts.ts` (tree).

**Exit (pending manual run):** code compiles and the helper contracts it calls are all tested;
needs an Extension Development Host (`F5`) smoke test to confirm activation → build → Problems →
artifact refresh in the live UI.

✅ **Audit defects in this extension all resolved 2026-06-22:** A2 (Program reports failure on
success), A5 (duplicate build tasks), A8 (multi-root/watcher gaps), A10 (artifact UI-thread walk),
A-minor (matcher regex). See AUDIT FINDINGS above.

---

## Phase 3 — Project authoring & config UX

- [ ] `CC5X: Create Project` wizard.
- [ ] Device picker backed by `list-devices --json`.
- [ ] Config editor (webview or quick-pick) using pack metadata for legal values.
- [ ] `CC5X: Sync Config` CodeLens over the managed config block.
- [ ] `CC5X: Generate Header` command.
- [ ] Manifest validation diagnostics (unknown config symbols, invalid values, missing header).

**Exit:** create a new PIC16F project without hand-editing JSON; legal config values selectable.

---

## Phase 4 — IntelliSense (editor-only, non-authoritative)

- [ ] Generate `generated/vscode/cc5x_intellisense.h` shim (`bit`, `bank0..3`, `persistent`, `interrupt`).
- [ ] Generate editor-only `compile_commands.json` (force-include shim, define `__CC5X__`, device macro).
- [ ] Settings helper for Microsoft C/C++ and/or Microchip clangd.
- [ ] `CC5X: Refresh IntelliSense` command.
- [ ] Validate shim against real CC5X examples; document residual false-positive noise.

**Exit:** `main.c` has useful symbols/navigation; CC5X build remains authoritative for correctness.

---

## Phase 5 — PICkit 4/5 programming integration (the payoff)

Primary path: shell out to `ipecmd.sh` (Linux-native, no extra extension dependency).

- [x] Add a `program` subcommand to the Python helper (`--action program|erase|verify|
      blank-check`) that builds the `ipecmd.sh -P<device> -TP<tool> -F<hex> -M` command line
      from a manifest (`--project`) or explicit `--device`/`--hex`, with auto-discovered
      IPECMD (`$CC5X_IPECMD` / system-wide MPLAB X), `--tool` (default `PK4`),
      `--release-from-reset` (`-OL`), `--ipe-arg` passthrough, `--dry-run`, and a `--json`
      contract (`{ok, action, device, tool, ipecmd, image, command}` / `{ok:false, error:{kind,…}}`).
      Flags verified against `Readme for IPECMD.htm`: program `-M`, verify `-Y`, erase `-E`,
      blank-check `-C`. Covered by `tests/test_build.py` (37 tests pass).
      Remaining: on a real (non-dry-run) `--json` invocation, IPECMD's own stdout streams
      before the JSON summary — capture it into the payload if the extension needs pure JSON.
      ✅ **RESOLVED (A2):** `--json` now captures IPECMD stdout/stderr into the payload, so the
      extension's Program command parses pure JSON and reports success correctly.
- [x] Generate a `CC5X: Program` VS Code task that runs after the build task
      (`dependsOn` + `dependsOrder: sequence`), pointed at the produced `.hex`.
- [x] Generate a matching `CC5X: Erase` task (plus Verify and Blank Check).
      → New `vscode-tasks` subcommand renders `.vscode/tasks.json` from a manifest: per-edition
      `Build` (first = default build), `Program`, `Verify`, plus device-level `Erase` and
      `Blank Check`. Uses `type: process` (no shell quoting). **Merges safely**: preserves the
      user's non-`CC5X:` tasks, replaces stale `CC5X:` ones (idempotent), and refuses to clobber
      an invalid existing `tasks.json` without `--force`. Options: `--tool`, `--python`,
      `--helper`, `--problem-matcher`, `--manifest`, `--output`, `--stdout`. Schema verified
      against VS Code docs (version 2.0.0, group/dependsOn/dependsOrder). Covered by
      `tests/test_build.py` (42 tests pass).
      ✅ **RESOLVED (A3):** `strip_jsonc()` normalizes JSONC before parsing; `--force` backs up an
      unparseable file rather than discarding user tasks.
      ✅ **RESOLVED (A5):** the extension provider no longer contributes Build tasks; generated
      `tasks.json` is the single source, so no duplicates.
- [ ] Map device → `-P` arg from the manifest; tool → `-TP` code via `cc5x.programmerTool`
      setting (`PK4`/`PPK5`/`SNAP`/`ICD4`/...). Default `PK4`.
- [ ] Surface `ipecmd` exit code / output in the Problems/output channel; clear guidance
      when no tool is detected, USB perms missing, or device unsupported.
- [ ] Make the IPECMD path a setting (`cc5x.ipecmdPath`), defaulting to the detected
      `/opt/microchip/mplabx/v6.30/mplab_platform/mplab_ipe/ipecmd.sh`.

Alternative path (only if the IPE GUI/adapter is preferred):

- [ ] Generate a `MPLAB-DebugAdapter` `program`/`erase` task (`tool: "PICkit 4"`,
      `device`, `image` from manifest) for users who want the Microchip extension flow.

**Exit:** build task → program task flashes the PIC via PICkit 4 entirely inside VS Code.

---

## Phase 5b — Curiosity Nano on-board debugger support (PIC10/12/16)

Goal: let the same build→program flow target a **Microchip Curiosity Nano** board's on-board
debugger over a single USB cable — no separate PICkit needed.

### Confirmed facts (Microchip MCP, 2026-06-22)

- PIC16F **Curiosity Nano** kits exist and are stocked, e.g. `DM164144` (PIC16F18446),
  `DM164148` (PIC16F15376), `EV09Z19A` (PIC16F15244), `EV35F40A` (PIC16F15276),
  `EV53Z50A` (PIC16F18076). Each has an **on-board debugger** that programs/debugs the
  on-board PIC via **ICSP**, with **MPLAB X** as front-end → drivable by IPECMD/MDB.
- The on-board debugger can **also program external microcontrollers** (per the board user
  guides' "Programming External Microcontrollers" section) — so a Nano can flash an external
  PIC10F/PIC12F target too.
- Relevant IPECMD `-TP` on-board tool codes that exist (verified in `Readme for IPECMD.htm`):
  `PKOB`, `PKOB4`, `NEDBG` (nEDBG is the AVR Curiosity Nano on-board debugger).
- `PIC16F18446` is already in our compiler-validated set → the DM164144 Nano is a ready
  end-to-end test target.

### Open questions to confirm (do NOT assume)

- [ ] **Confirm the exact IPECMD `-TP` tool code for a PIC Curiosity Nano's on-board debugger**
      (candidate: `PKOB`/`PKOB4`/`NEDBG`). The IPECMD readme doesn't map PIC-Nano → code; confirm
      from the specific board's user guide or by enumerating the connected tool (`ipecmd` lists
      the detected tool). Best done with hardware attached.
- [ ] Confirm whether dedicated **PIC10F/PIC12F** Curiosity Nano boards exist, or whether those
      families are only reachable as the *external* target of a Nano/PICkit (catalog search
      surfaced PIC16F/PIC18F Nano kits only). Verify via Microchip MCP before claiming either way.
- [ ] Confirm which Nano on-board PIC devices CC5X actually supports (CC5X covers
      PIC10F/12F/16F; check each Nano's MCU against `SUPPORT_MATRIX.md` — some enhanced-midrange
      Q-series parts may be out of scope).

### Implementation (mostly already enabled by the `program` subcommand)

- [ ] The `program` subcommand already accepts any `-TP` code via `--tool`, so once the Nano
      code is confirmed, `program --tool <code>` works with no code change. Add the confirmed
      code(s) to the documented `--tool` choices and the `cc5x.programmerTool` setting enum.
- [ ] Add tool **auto-detection**: run IPECMD's tool-list/detect to pick the connected on-board
      debugger automatically, so a Nano "just works" without the user knowing the code.
- [ ] Manifest support: allow a per-project default tool (e.g. `"tool": "PKOB"` in
      `setcc-native.json`) so a Nano-based project records its programmer; have `program` and
      `vscode-tasks` honor it.
- [ ] Doc: a short "Using a Curiosity Nano" section (one USB cable; on-board ICSP; can also
      program an external PIC10F/12F target).

**Exit:** `program --tool <nano-code>` (or auto-detected) flashes the on-board PIC of a
Curiosity Nano from the same build output; documented and, ideally, verified on `DM164144`
(PIC16F18446, already validated).

---

## Phase 6 — Testing & sample projects

- [ ] Fixture projects: PIC10F200, PIC12F1840, PIC16F1509, PIC16F15313.
- [ ] Per-fixture: build success, artifact detection, diagnostics on intentional error, config legal values.
- [ ] Extension unit tests: manifest parsing, diagnostic conversion, task generation.
- [ ] VS Code extension-host integration test (open fixture, discover, doctor, provide tasks, simulate build).
- [ ] Run `/code-review` on the diff before calling any phase done.

---

## Research / experimental (kept under `experimental/`, unsupported)

- [ ] `.mplab.json` sidecar generator; test Microchip Project View + RunCMake behavior; decide default/opt-in/abandon.
- [ ] Toolchain-provider patch PoC (direct `registerCustomToolchain` descriptor bypass — minified JS, version-fragile).
- [ ] Private-service provider experiment (`contributes.mplab.providers`) — research-grade only.
- [ ] Debug capability matrix via `mdb.sh` (and/or the VS Code Debug Adapter): can either
      consume CC5X `.cof`/`.cod` for source stepping, watch, register view, PC→source mapping
      on PICkit 4? (program-only / asm / source / simulator / hardware / unsupported).

---

## Known hard constraints (don't gate a release on these)

- First-class MPLAB toolchain registration is **blocked**: `microchip.toolchains` scans a
  hardcoded `SUPPORTED_COMPILERS` list that excludes `cc5x`; enabling it requires patching
  minified vendor JS that updates overwrite.
- Source-level debugging is **unproven** until the `.cof`/`.cod` acceptance test passes.
- CC5X dialect (unsigned `char`, 8-bit `int`, 16-bit `long`, static locals, no auto RAM init)
  means clang-based IntelliSense is best-effort, never authoritative.
