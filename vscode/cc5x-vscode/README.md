# CC5X Tools (VS Code extension)

MVP companion extension that drives the repo's `tools/cc5x_setcc_native.py` helper to build
and program PIC10F/12F/16F projects with the CC5X compiler and MPLAB IPECMD (PICkit 4/5).

It does **not** reimplement the toolchain — every action shells out to the Python helper, which
remains the source of truth for pack discovery, header generation, config, builds, and
programming.

## Features (MVP)

- **Activation** when the workspace contains `setcc-native.json`.
- **CC5X: Doctor** — runs `doctor --json`, reports readiness in the status bar + output channel.
- **CC5X: Build** — builds a chosen edition, streams output, parses CC5X diagnostics into the
  Problems panel, and refreshes the artifact view.
- **CC5X: Program Device** — programs a built `.hex` via IPECMD (with a confirmation prompt).
- **CC5X: Select Device** — pick a target PIC from the locally discovered packs
  (`list-devices --json`) and write it to the manifest (`project-edit --device`).
- **CC5X: Generate VS Code Tasks** — writes `.vscode/tasks.json` (build + program/erase/verify).
- **Task support** — `CC5X: Generate VS Code Tasks` writes concrete build/program/verify/erase
  tasks to `.vscode/tasks.json`; the `type: cc5x` provider only resolves hand-authored tasks.
- **CC5X Artifacts** view — lists `.hex/.asm/.occ/.var/.fcs/.cpr/.cod/.cof` outputs.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `cc5x.pythonPath` | `python3` | Interpreter for the helper. |
| `cc5x.helperPath` | `tools/cc5x_setcc_native.py` | Workspace-relative helper path. |
| `cc5x.manifest` | `setcc-native.json` | Workspace-relative manifest path. |
| `cc5x.programmerTool` | `PK4` | IPECMD `-TP` code (PK4=PICkit 4, PK5=PICkit 5, SNAP, ICD4). |

## Develop

```bash
npm install
npm run compile      # or: npm run watch
```

Press `F5` in VS Code to launch an Extension Development Host. Open a folder that contains a
`setcc-native.json` manifest to activate the extension.
```
