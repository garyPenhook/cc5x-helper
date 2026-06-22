import * as vscode from 'vscode';
import * as path from 'path';
import {
  HelperResult,
  ProjectInfo,
  loadProject,
  manifestAbsPath,
  manifestWatchPattern,
  programmerTool,
  runHelper,
  runHelperJson,
  workspaceRoot,
} from './helper';
import { publishCc5xDiagnostics } from './diagnostics';
import { ArtifactsProvider } from './artifacts';
import { Cc5xTaskProvider } from './tasks';

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

  context.subscriptions.push(
    channel,
    diagnostics,
    statusBar,
    vscode.window.registerTreeDataProvider('cc5xArtifacts', artifacts),
    vscode.commands.registerCommand('cc5x.doctor', () => runDoctor(root)),
    vscode.commands.registerCommand('cc5x.build', () => runBuild(root)),
    vscode.commands.registerCommand('cc5x.program', () => runProgram(root)),
    vscode.commands.registerCommand('cc5x.generateTasks', () => generateTasks(root)),
    vscode.commands.registerCommand('cc5x.refreshArtifacts', () => artifacts.refresh()),
    vscode.commands.registerCommand('cc5x.openArtifact', (uri: vscode.Uri) =>
      vscode.window.showTextDocument(uri),
    ),
  );

  if (root) {
    context.subscriptions.push(
      vscode.tasks.registerTaskProvider('cc5x', new Cc5xTaskProvider(root)),
    );
    const watcher = vscode.workspace.createFileSystemWatcher(manifestWatchPattern(root));
    watcher.onDidChange(() => reloadProject(root));
    watcher.onDidCreate(() => reloadProject(root));
    // Clear the cached project when the manifest is deleted so stale data is not reused.
    watcher.onDidDelete(() => {
      currentProject = undefined;
    });
    context.subscriptions.push(watcher);
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
