import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { spawn } from 'child_process';

/** Result of running the CC5X Python helper. */
export interface HelperResult {
  code: number;
  stdout: string;
  stderr: string;
}

/** Minimal shape of a parsed project manifest (from `project-show --json`). */
export interface ProjectInfo {
  device: string;
  main_source: string;
  config_source: string;
  header: { mode: string; path: string };
  editions: Record<string, unknown>;
}

/** One discoverable device, as emitted by `list-devices --json`. */
export interface DeviceInfo {
  device: string;
  pack_family: string;
  pack_version: string;
  pack_root: string;
  pdsc: string | null;
}

/** One legal value for a config symbol, from `list-pack-config --device <dev> --json`. */
export interface ConfigOption {
  register: number;
  mask: string;
  state: string;
  comment: string;
}

/**
 * Legal config symbols for a device, keyed by symbol name (e.g. `BOREN`), each mapping
 * to its allowed states. This is the pack-derived source of truth the config editor
 * offers the user — only states present here are legal for the device.
 */
export type PackConfig = Record<string, ConfigOption[]>;

/** One edition's stored config + build options (from `project-show --edition <e> --json`). */
export interface EditionConfig {
  name: string;
  config: Record<string, string>;
  build_options: string[];
}

function config(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration('cc5x');
}

/**
 * The workspace folder that owns the CC5X project, or undefined if none is open.
 *
 * In a multi-root workspace the manifest may live in any folder, not folders[0]
 * (audit A8), so prefer the folder that actually contains the configured manifest;
 * fall back to the first folder when none matches.
 */
export function workspaceRoot(): vscode.WorkspaceFolder | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    return undefined;
  }
  const rel = manifestRelPath();
  if (path.isAbsolute(rel)) {
    const owner = folders.find((f) => rel.startsWith(f.uri.fsPath + path.sep));
    return owner ?? folders[0];
  }
  const owner = folders.find((f) => fs.existsSync(path.join(f.uri.fsPath, rel)));
  return owner ?? folders[0];
}

/**
 * Glob pattern that watches the manifest file. Handles an absolute `cc5x.manifest`,
 * which would otherwise break `new RelativePattern(root, abs)` (audit A8).
 */
export function manifestWatchPattern(root: vscode.WorkspaceFolder): vscode.RelativePattern {
  const rel = manifestRelPath();
  if (path.isAbsolute(rel)) {
    return new vscode.RelativePattern(path.dirname(rel), path.basename(rel));
  }
  return new vscode.RelativePattern(root, rel);
}

export function pythonPath(): string {
  return config().get<string>('pythonPath', 'python3');
}

/** Absolute path to the Python helper, resolved against the workspace root. */
export function helperPath(root: vscode.WorkspaceFolder): string {
  const configured = config().get<string>('helperPath', 'tools/cc5x_setcc_native.py');
  return path.isAbsolute(configured) ? configured : path.join(root.uri.fsPath, configured);
}

/** Workspace-relative manifest path (as the user configured it). */
export function manifestRelPath(): string {
  return config().get<string>('manifest', 'setcc-native.json');
}

/** Absolute manifest path resolved against the workspace root. */
export function manifestAbsPath(root: vscode.WorkspaceFolder): string {
  const rel = manifestRelPath();
  return path.isAbsolute(rel) ? rel : path.join(root.uri.fsPath, rel);
}

export function programmerTool(): string {
  return config().get<string>('programmerTool', 'PK4');
}

/** Timeout (ms) before a spawned helper/IPECMD child is killed; 0 disables. */
export function commandTimeoutMs(): number {
  const seconds = config().get<number>('commandTimeoutSeconds', 300);
  return seconds > 0 ? seconds * 1000 : 0;
}

/**
 * Run the CC5X helper with the given subcommand args.
 * Uses spawn (no shell) so arguments are passed without quoting issues.
 * Output is streamed to `channel` when provided, and also captured.
 */
export function runHelper(
  root: vscode.WorkspaceFolder,
  args: string[],
  channel?: vscode.OutputChannel,
): Promise<HelperResult> {
  const command = pythonPath();
  const fullArgs = [helperPath(root), ...args];
  if (channel) {
    channel.appendLine(`$ ${command} ${fullArgs.join(' ')}`);
  }
  const timeoutMs = commandTimeoutMs();
  return new Promise<HelperResult>((resolve, reject) => {
    const child = spawn(command, fullArgs, { cwd: root.uri.fsPath });
    let stdout = '';
    let stderr = '';
    let settled = false;
    let timer: NodeJS.Timeout | undefined;
    // Settle exactly once and always clear the timer / kill a runaway child, so a hung
    // python/IPECMD (e.g. waiting on absent hardware) can never leave the command
    // pending forever or orphan the process.
    const finish = (action: () => void): void => {
      if (settled) {
        return;
      }
      settled = true;
      if (timer) {
        clearTimeout(timer);
      }
      action();
    };
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        child.kill('SIGTERM');
        finish(() =>
          reject(new Error(`CC5X command timed out after ${timeoutMs / 1000}s: ${command} ${fullArgs.join(' ')}`)),
        );
      }, timeoutMs);
    }
    child.stdout.on('data', (data: Buffer) => {
      const text = data.toString();
      stdout += text;
      channel?.append(text);
    });
    child.stderr.on('data', (data: Buffer) => {
      const text = data.toString();
      stderr += text;
      channel?.append(text);
    });
    child.on('error', (err) => finish(() => reject(err)));
    child.on('close', (code) => finish(() => resolve({ code: code ?? -1, stdout, stderr })));
  });
}

/** Run a helper subcommand with `--json` and parse stdout as JSON. */
export async function runHelperJson<T>(
  root: vscode.WorkspaceFolder,
  args: string[],
): Promise<T> {
  const result = await runHelper(root, [...args, '--json']);
  try {
    return JSON.parse(result.stdout) as T;
  } catch (err) {
    throw new Error(
      `CC5X helper did not return valid JSON for "${args.join(' ')}" ` +
        `(exit ${result.code}): ${result.stderr || result.stdout}`,
    );
  }
}

/** Load and parse the project manifest via the helper. */
export function loadProject(root: vscode.WorkspaceFolder): Promise<ProjectInfo> {
  return runHelperJson<ProjectInfo>(root, [
    'project-show',
    '--project',
    manifestAbsPath(root),
  ]);
}

/**
 * List the locally discoverable CC5X-family devices (PIC10F/12F/16F) from installed
 * packs, via `list-devices --json`. The helper already merges archive + installed
 * packs keeping the highest pack version, so the result is deduplicated and sorted.
 */
export function listDevices(root: vscode.WorkspaceFolder): Promise<DeviceInfo[]> {
  return runHelperJson<DeviceInfo[]>(root, ['list-devices']);
}

/**
 * List the legal dynamic-config symbols and their allowed states for a device, straight
 * from pack metadata (`list-pack-config --device <dev> --json`). Used by the config editor
 * to constrain the user to values the device actually supports.
 */
export function listPackConfig(
  root: vscode.WorkspaceFolder,
  device: string,
): Promise<PackConfig> {
  return runHelperJson<PackConfig>(root, ['list-pack-config', '--device', device]);
}

/** Load one edition's stored config via `project-show --edition <e> --json`. */
export function loadEditionConfig(
  root: vscode.WorkspaceFolder,
  edition: string,
): Promise<EditionConfig> {
  return runHelperJson<EditionConfig>(root, [
    'project-show',
    '--project',
    manifestAbsPath(root),
    '--edition',
    edition,
  ]);
}

/**
 * Set (`state` given) or remove (`state` null) one config symbol for an edition via
 * `project-set-config`. Removing an override lets the device fall back to its default.
 */
export function updateEditionConfig(
  root: vscode.WorkspaceFolder,
  edition: string,
  name: string,
  state: string | null,
): Promise<HelperResult> {
  const args = ['project-set-config', '--project', manifestAbsPath(root), '--edition', edition];
  if (state === null) {
    args.push('--remove', name);
  } else {
    args.push('--set', `${name}=${state}`);
  }
  return runHelper(root, args);
}

/** Set the manifest's target device via `project-edit --device` (validates + writes). */
export function setProjectDevice(
  root: vscode.WorkspaceFolder,
  device: string,
): Promise<HelperResult> {
  return runHelper(root, [
    'project-edit',
    '--project',
    manifestAbsPath(root),
    '--device',
    device,
  ]);
}

/** Options for {@link initProject}. */
export interface InitProjectOptions {
  device: string;
  main: string;
  headerMode: string;
  /** Pass `--force` to overwrite an existing manifest (project-init refuses otherwise). */
  force?: boolean;
}

/**
 * Create a manifest at the configured path via `project-init`. The helper refuses to
 * overwrite an existing file unless `--force` is set, surfaced here via `opts.force`.
 */
export function initProject(
  root: vscode.WorkspaceFolder,
  opts: InitProjectOptions,
): Promise<HelperResult> {
  const args = [
    'project-init',
    '--project',
    manifestAbsPath(root),
    '--device',
    opts.device,
    '--main',
    opts.main,
    '--header-mode',
    opts.headerMode,
  ];
  if (opts.force) {
    args.push('--force');
  }
  return runHelper(root, args);
}
