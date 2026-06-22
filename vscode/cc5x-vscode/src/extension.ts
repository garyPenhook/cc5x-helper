import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import {
  DeviceInfo,
  HelperResult,
  ProjectInfo,
  initProject,
  listDevices,
  listPackConfig,
  PackConfig,
  loadEditionConfig,
  loadProject,
  manifestAbsPath,
  manifestWatchPattern,
  programmerTool,
  runHelper,
  runHelperJson,
  setProjectDevice,
  syncProjectConfig,
  updateEditionConfig,
  workspaceRoot,
} from './helper';
import { publishCc5xDiagnostics } from './diagnostics';
import { ArtifactsProvider } from './artifacts';
import { Cc5xTaskProvider } from './tasks';
import { Cc5xConfigLensProvider } from './configLens';

interface DoctorReport {
  ready: boolean;
  pack_device_count: number;
  validated_device_count: number;
  compiler: string | null;
  runner: string | null;
}

interface ProgramResult {
  ok: boolean;
  returncode?: number;
  stdout?: string;
  stderr?: string;
  error?: { kind: string; message: string };
}

let channel: vscode.OutputChannel;
let diagnostics: vscode.DiagnosticCollection;
let artifacts: ArtifactsProvider;
let statusBar: vscode.StatusBarItem;
let currentProject: ProjectInfo | undefined;
let configLens: Cc5xConfigLensProvider;

export function activate(context: vscode.ExtensionContext): void {
  const root = workspaceRoot();
  channel = vscode.window.createOutputChannel('CC5X');
  diagnostics = vscode.languages.createDiagnosticCollection('cc5x');
  artifacts = new ArtifactsProvider();
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = 'cc5x.doctor';
  statusBar.text = '$(circuit-board) CC5X';
  statusBar.tooltip = 'CC5X: run Doctor';
  statusBar.show();
  configLens = new Cc5xConfigLensProvider(
    () => root,
    () => currentProject,
  );

  context.subscriptions.push(
    channel,
    diagnostics,
    statusBar,
    vscode.window.registerTreeDataProvider('cc5xArtifacts', artifacts),
    vscode.commands.registerCommand('cc5x.doctor', () => runDoctor(root)),
    vscode.commands.registerCommand('cc5x.build', () => runBuild(root)),
    vscode.commands.registerCommand('cc5x.program', () => runProgram(root)),
    vscode.commands.registerCommand('cc5x.generateTasks', () => generateTasks(root)),
    vscode.commands.registerCommand('cc5x.createProject', () => createProject(root)),
    vscode.commands.registerCommand('cc5x.selectDevice', () => selectDevice(root)),
    vscode.commands.registerCommand('cc5x.editConfig', () => editConfig(root)),
    vscode.commands.registerCommand('cc5x.syncConfig', (uri?: vscode.Uri) => syncConfig(root, uri)),
    vscode.languages.registerCodeLensProvider({ language: 'c' }, configLens),
    vscode.commands.registerCommand('cc5x.refreshArtifacts', () => artifacts.refresh()),
    vscode.commands.registerCommand('cc5x.openArtifact', (uri: vscode.Uri) => {
      // Binary artifacts (.hex/.cod/.cof) can't open as text; surface the error
      // instead of leaving an unhandled promise rejection.
      void vscode.window.showTextDocument(uri).then(undefined, (err) =>
        vscode.window.showErrorMessage(`CC5X: could not open ${uri.fsPath}: ${String(err)}`),
      );
    }),
  );

  if (root) {
    context.subscriptions.push(
      vscode.tasks.registerTaskProvider('cc5x', new Cc5xTaskProvider(root)),
    );
    const watcher = vscode.workspace.createFileSystemWatcher(manifestWatchPattern(root));
    context.subscriptions.push(
      watcher,
      watcher.onDidChange(() => void reloadProject(root)),
      watcher.onDidCreate(() => void reloadProject(root)),
      // Clear the cached project when the manifest is deleted so stale data is not reused,
      // and re-evaluate the config lens (it must vanish once there is no project).
      watcher.onDidDelete(() => {
        currentProject = undefined;
        configLens.refresh();
      }),
    );
    // Only auto-run the helper (which executes the workspace-configured interpreter)
    // once the workspace is trusted; re-run when trust is granted.
    if (vscode.workspace.isTrusted) {
      void reloadProject(root);
    }
    context.subscriptions.push(
      vscode.workspace.onDidGrantWorkspaceTrust(() => reloadProject(root)),
    );
  }
}

export function deactivate(): void {
  // Subscriptions are disposed by VS Code via context.subscriptions.
}

function requireRoot(root: vscode.WorkspaceFolder | undefined): root is vscode.WorkspaceFolder {
  if (!root) {
    vscode.window.showErrorMessage('CC5X: open a folder containing setcc-native.json.');
    return false;
  }
  return true;
}

/**
 * Refuse to execute anything in an untrusted workspace.
 *
 * `capabilities.untrustedWorkspaces.supported = false` already prevents activation
 * until the workspace is trusted; this is a defense-in-depth guard so the helper /
 * IPECMD are never launched with workspace-controlled `pythonPath`/`helperPath` even
 * if a command is somehow reached before trust is granted.
 */
function ensureTrusted(): boolean {
  if (!vscode.workspace.isTrusted) {
    vscode.window.showErrorMessage(
      'CC5X commands are disabled in an untrusted workspace. Trust this folder to enable build/program.',
    );
    return false;
  }
  return true;
}

async function reloadProject(root: vscode.WorkspaceFolder): Promise<void> {
  try {
    currentProject = await loadProject(root);
    const sourceDir = path.dirname(path.join(root.uri.fsPath, currentProject.main_source));
    artifacts.setSearchDirs([sourceDir, path.join(root.uri.fsPath, 'build')]);
  } catch (err) {
    currentProject = undefined;
    channel.appendLine(`CC5X: could not load manifest: ${String(err)}`);
  }
  // The config_source path may have changed; re-evaluate the Sync Config lens.
  configLens.refresh();
}

async function runDoctor(root: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  channel.show(true);
  try {
    const report = await runHelperJson<DoctorReport>(root, ['doctor']);
    statusBar.text = report.ready ? '$(circuit-board) CC5X: ready' : '$(circuit-board) CC5X: not ready';
    const summary =
      `ready=${report.ready}, devices=${report.pack_device_count}, ` +
      `validated=${report.validated_device_count}`;
    channel.appendLine(`CC5X Doctor: ${summary}`);
    if (report.ready) {
      vscode.window.showInformationMessage(`CC5X ready (${report.pack_device_count} devices).`);
    } else {
      vscode.window.showWarningMessage(`CC5X not ready: ${summary}. See the CC5X output channel.`);
    }
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X Doctor failed: ${String(err)}`);
  }
}

async function pickEdition(
  root: vscode.WorkspaceFolder,
  placeholder: string,
): Promise<string | undefined> {
  if (!currentProject) {
    await reloadProject(root);
  }
  const editions = currentProject ? Object.keys(currentProject.editions).sort() : [];
  if (editions.length === 0) {
    vscode.window.showErrorMessage('CC5X: no editions found in the manifest.');
    return undefined;
  }
  if (editions.length === 1) {
    return editions[0];
  }
  return vscode.window.showQuickPick(editions, { placeHolder: placeholder });
}

/**
 * Create-project wizard: pick a device (#12 picker), the main source file, and the
 * header mode, then run `project-init` to write the manifest. Handles the
 * already-exists case (project-init refuses without --force) by confirming an
 * overwrite, and scaffolds + opens the main source file so the user has a start point.
 */
async function createProject(root: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  const device = await pickDevice(root, 'Select the target PIC device for the new project');
  if (!device) {
    return;
  }
  const mainInput = await vscode.window.showInputBox({
    prompt: 'Main C source file (relative to the workspace)',
    value: 'main.c',
    validateInput: (v) => (v.trim().length === 0 ? 'Enter a file name' : undefined),
  });
  if (mainInput === undefined) {
    return;
  }
  const main = mainInput.trim();
  const headerMode = await vscode.window.showQuickPick(
    [
      { label: 'generated', description: 'Generate the device header from packs (default)' },
      { label: 'supplied', description: 'Use a CC5X-supplied <device>.H next to the compiler' },
      { label: 'existing', description: 'Use an existing header file you provide' },
    ],
    { placeHolder: 'Header mode' },
  );
  if (!headerMode) {
    return;
  }

  // project-init refuses to overwrite an existing manifest without --force.
  const manifestPath = manifestAbsPath(root);
  let force = false;
  if (fs.existsSync(manifestPath)) {
    const overwrite = await vscode.window.showWarningMessage(
      `${path.basename(manifestPath)} already exists. Overwrite it?`,
      { modal: true },
      'Overwrite',
    );
    if (overwrite !== 'Overwrite') {
      return;
    }
    force = true;
  }

  let result: HelperResult;
  try {
    result = await initProject(root, { device, main, headerMode: headerMode.label, force });
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X: could not create project: ${String(err)}`);
    return;
  }
  if (result.code !== 0) {
    channel.show(true);
    channel.appendLine(result.stdout + result.stderr);
    vscode.window.showErrorMessage('CC5X: failed to create project. See the CC5X output channel.');
    return;
  }

  // Refresh the cached manifest immediately (the watcher also fires on the new file).
  await reloadProject(root);

  // Scaffold the main source file if missing, then open it as a starting point. The
  // file is created empty (no assumed device/dialect content) for the user to fill.
  const mainAbs = path.isAbsolute(main) ? main : path.join(root.uri.fsPath, main);
  try {
    if (!fs.existsSync(mainAbs)) {
      fs.mkdirSync(path.dirname(mainAbs), { recursive: true });
      fs.writeFileSync(mainAbs, '');
    }
    await vscode.window.showTextDocument(vscode.Uri.file(mainAbs));
  } catch (err) {
    channel.appendLine(`CC5X: created project but could not open ${mainAbs}: ${String(err)}`);
  }
  vscode.window.showInformationMessage(
    `CC5X: created project for ${device} (${headerMode.label}).`,
  );
}

/**
 * Show a QuickPick over the locally discoverable CC5X devices (`list-devices --json`)
 * and return the chosen device name, or undefined if cancelled / none found.
 *
 * Exported so a future Create Project wizard can reuse the same picker. The caller is
 * responsible for the trust/root guards; this only talks to the (read-only) helper.
 */
export async function pickDevice(
  root: vscode.WorkspaceFolder,
  placeholder = 'Select a target PIC device',
): Promise<string | undefined> {
  let devices: DeviceInfo[];
  try {
    devices = await listDevices(root);
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X: could not list devices: ${String(err)}`);
    return undefined;
  }
  if (devices.length === 0) {
    vscode.window.showErrorMessage('CC5X: no devices found. Run Doctor to check pack discovery.');
    return undefined;
  }
  const items: vscode.QuickPickItem[] = devices.map((d) => ({
    label: d.device,
    description: `${d.pack_family} ${d.pack_version}`,
  }));
  // matchOnDescription lets the user filter by family ("16F") or pack version too.
  const picked = await vscode.window.showQuickPick(items, {
    placeHolder: placeholder,
    matchOnDescription: true,
  });
  return picked?.label;
}

/** Pick a device and write it to the current manifest via `project-edit --device`. */
async function selectDevice(root: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  const device = await pickDevice(root);
  if (!device) {
    return;
  }
  let result: HelperResult;
  try {
    result = await setProjectDevice(root, device);
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X: could not set device: ${String(err)}`);
    return;
  }
  if (result.code === 0) {
    // Refresh the cached manifest so the new device is reflected immediately
    // (the manifest watcher also fires, but this avoids a visible lag).
    await reloadProject(root);
    vscode.window.showInformationMessage(`CC5X: project device set to ${device}.`);
  } else {
    channel.show(true);
    channel.appendLine(result.stdout + result.stderr);
    vscode.window.showErrorMessage(
      `CC5X: failed to set device to ${device}. See the CC5X output channel.`,
    );
  }
}

/**
 * Config editor (quick-pick): edit an edition's dynamic-config symbols, constrained to the
 * legal states the device's pack metadata allows (Phase 3). Loops so several symbols can be
 * set in one session — Esc at the symbol list finishes. Each change is written immediately
 * via `project-set-config` (set NAME=STATE, or remove the override to fall back to the
 * device default).
 */
async function editConfig(root: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  const edition = await pickEdition(root, 'Select edition to configure');
  if (!edition) {
    return;
  }
  // The device determines which symbols/values are legal. pickEdition already loaded the
  // manifest into currentProject, so read the device straight from the cached snapshot.
  const device = currentProject?.device;
  if (!device) {
    vscode.window.showErrorMessage('CC5X: no device set in the manifest. Select a device first.');
    return;
  }
  let symbols: PackConfig;
  try {
    symbols = await listPackConfig(root, device);
  } catch (err) {
    vscode.window.showErrorMessage(
      `CC5X: could not load config metadata for ${device}: ${String(err)}`,
    );
    return;
  }
  const names = Object.keys(symbols).sort();
  if (names.length === 0) {
    vscode.window.showInformationMessage(`CC5X: ${device} exposes no dynamic config symbols.`);
    return;
  }

  // Pick a symbol → pick a legal value → write → repeat. Re-read the edition config each pass
  // so the displayed "current" values reflect writes made earlier in the same session.
  for (;;) {
    let current: Record<string, string>;
    try {
      current = (await loadEditionConfig(root, edition)).config;
    } catch (err) {
      vscode.window.showErrorMessage(`CC5X: could not read edition config: ${String(err)}`);
      return;
    }
    const symbolItems: vscode.QuickPickItem[] = names.map((name) => {
      const value = current[name];
      const option = value ? symbols[name].find((o) => o.state === value) : undefined;
      return {
        label: name,
        description: value ? `= ${value}` : '(device default)',
        detail: option?.comment || undefined,
      };
    });
    const pickedSymbol = await vscode.window.showQuickPick(symbolItems, {
      placeHolder: `Edit ${edition} config for ${device} — pick a symbol (Esc to finish)`,
      matchOnDescription: true,
      matchOnDetail: true,
    });
    if (!pickedSymbol) {
      break;
    }
    const name = pickedSymbol.label;
    const currentValue = current[name];
    const RESET_LABEL = '$(discard) Reset to device default';
    const valueItems: vscode.QuickPickItem[] = symbols[name].map((o) => ({
      label: o.state,
      description: o.state === currentValue ? `${o.comment}  ● current` : o.comment,
    }));
    if (currentValue !== undefined) {
      valueItems.unshift({ label: RESET_LABEL, description: `Remove the ${name} override` });
    }
    const pickedValue = await vscode.window.showQuickPick(valueItems, {
      placeHolder: `${name}: select a value`,
      matchOnDescription: true,
    });
    if (!pickedValue) {
      continue; // back to the symbol list without changing anything
    }
    const state = pickedValue.label === RESET_LABEL ? null : pickedValue.label;
    let result: HelperResult;
    try {
      result = await updateEditionConfig(root, edition, name, state);
    } catch (err) {
      vscode.window.showErrorMessage(`CC5X: could not update config: ${String(err)}`);
      return;
    }
    if (result.code !== 0) {
      channel.show(true);
      channel.appendLine(result.stdout + result.stderr);
      vscode.window.showErrorMessage(`CC5X: failed to set ${name}. See the CC5X output channel.`);
      return;
    }
    // No explicit reload here: the next pass reads the edition config fresh from disk via
    // loadEditionConfig, and the manifest watcher refreshes currentProject for everything else.
  }
  vscode.window.showInformationMessage(`CC5X: finished editing ${edition} config.`);
}

/**
 * Regenerate the managed config block in the project's config source from an edition's
 * stored config (Phase 3). Backs the "CC5X: Sync Config" CodeLens and the command palette.
 * The helper rewrites the file on disk, so any unsaved edits to that file are saved first
 * (otherwise sync-config would read the stale on-disk copy and the editor would then revert
 * over the user's changes).
 */
async function syncConfig(
  root: vscode.WorkspaceFolder | undefined,
  uri?: vscode.Uri,
): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  const edition = await pickEdition(root, 'Select edition to sync config from');
  if (!edition) {
    return;
  }
  // Save the config source if it is open with unsaved edits, so the helper rewrites the
  // latest content. Prefer the lens-supplied URI; fall back to the manifest's config_source.
  const targetPath =
    uri?.fsPath ??
    (currentProject
      ? path.isAbsolute(currentProject.config_source)
        ? currentProject.config_source
        : path.join(root.uri.fsPath, currentProject.config_source)
      : undefined);
  if (targetPath) {
    // Normalize both sides: the palette fallback builds targetPath via path.join, which can
    // differ from the open document's fsPath by separators / '.' / '..' segments. A raw
    // string mismatch would skip the save and let the helper revert the editor over unsaved
    // edits — the exact hazard this guard exists to prevent.
    const target = path.normalize(targetPath);
    const doc = vscode.workspace.textDocuments.find(
      (d) => path.normalize(d.uri.fsPath) === target,
    );
    if (doc && doc.isDirty) {
      await doc.save();
    }
  }
  let result: HelperResult;
  try {
    result = await syncProjectConfig(root, edition);
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X: could not sync config: ${String(err)}`);
    return;
  }
  if (result.code === 0) {
    // The helper rewrote the file on disk; VS Code reverts the (now clean) editor to match.
    vscode.window.showInformationMessage(`CC5X: synced ${edition} config into the managed block.`);
  } else {
    channel.show(true);
    channel.appendLine(result.stdout + result.stderr);
    vscode.window.showErrorMessage('CC5X: failed to sync config. See the CC5X output channel.');
  }
}

async function runBuild(root: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  const edition = await pickEdition(root, 'Select edition to build');
  if (!edition) {
    return;
  }
  channel.show(true);
  let result: HelperResult;
  try {
    result = await runHelper(
      root,
      ['build', '--project', manifestAbsPath(root), '--edition', edition],
      channel,
    );
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X build could not start: ${String(err)}`);
    return;
  }

  const baseDir = currentProject
    ? path.dirname(path.join(root.uri.fsPath, currentProject.main_source))
    : root.uri.fsPath;
  const count = publishCc5xDiagnostics(result.stdout + '\n' + result.stderr, baseDir, diagnostics);
  artifacts.refresh();

  if (result.code === 0) {
    vscode.window.showInformationMessage(`CC5X build (${edition}) succeeded.`);
  } else {
    vscode.window.showErrorMessage(
      `CC5X build (${edition}) failed with ${count} diagnostic(s). See Problems / CC5X output.`,
    );
  }
}

async function runProgram(root: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  const edition = await pickEdition(root, 'Select edition to program');
  if (!edition) {
    return;
  }
  const tool = programmerTool();
  const confirm = await vscode.window.showWarningMessage(
    `Program the device with edition "${edition}" via ${tool}? This writes to hardware.`,
    { modal: true },
    'Program',
  );
  if (confirm !== 'Program') {
    return;
  }
  channel.show(true);
  try {
    const result = await runHelperJson<ProgramResult>(root, [
      'program',
      '--project',
      manifestAbsPath(root),
      '--edition',
      edition,
      '--tool',
      tool,
    ]);
    // IPECMD's own output is captured into the JSON payload (helper --json). Echo it to
    // the channel so the user can see the flash log even though it no longer streams.
    if (result.stdout) {
      channel.append(result.stdout);
    }
    if (result.stderr) {
      channel.append(result.stderr);
    }
    if (result.ok) {
      vscode.window.showInformationMessage(`CC5X: programmed ${edition} via ${tool}.`);
    } else {
      const message = result.error?.message ?? `IPECMD exited ${result.returncode ?? '?'}`;
      vscode.window.showErrorMessage(`CC5X program failed: ${message}. See the CC5X output channel.`);
    }
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X program failed: ${String(err)}`);
  }
}

async function generateTasks(root: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!ensureTrusted() || !requireRoot(root)) {
    return;
  }
  channel.show(true);
  try {
    const result = await runHelper(
      root,
      ['vscode-tasks', '--project', manifestAbsPath(root), '--tool', programmerTool()],
      channel,
    );
    if (result.code === 0) {
      vscode.window.showInformationMessage('CC5X: generated .vscode/tasks.json.');
    } else {
      vscode.window.showErrorMessage('CC5X: failed to generate tasks.json. See CC5X output.');
    }
  } catch (err) {
    vscode.window.showErrorMessage(`CC5X generate tasks failed: ${String(err)}`);
  }
}
