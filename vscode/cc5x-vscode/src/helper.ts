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

/** The configured helper path as-is (relative), for embedding in workspace-relative tasks. */
export function helperRelPath(): string {
  return config().get<string>('helperPath', 'tools/cc5x_setcc_native.py');
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

/** Directory the manifest lives in — the base for its manifest-relative paths. */
export function manifestDir(root: vscode.WorkspaceFolder): string {
  return path.dirname(manifestAbsPath(root));
}

/**
 * Resolve a manifest-relative project path (main_source / config_source / header) the way
 * the CLI's `project_path_join` does: an absolute value is used as-is, otherwise it resolves
 * against the **manifest's own directory** — NOT the workspace root, which differs when the
 * manifest lives in a subdirectory. Resolving against the workspace root would point the
 * artifact view, the diagnostics base dir, and the Sync-Config lens at the wrong files
 * (audit #4 / #4b), and `path.join(workspaceRoot, absolutePath)` would mangle an absolute
 * source path outright.
 */
export function resolveManifestRelative(root: vscode.WorkspaceFolder, p: string): string {
  return path.isAbsolute(p) ? p : path.join(manifestDir(root), p);
}

export function programmerTool(): string {
  return config().get<string>('programmerTool', 'PK4');
}

/**
 * Explicit IPECMD launcher path, or '' to let the helper auto-discover it (from
 * `$CC5X_IPECMD` or the newest installed MPLAB X). Passed to `program` as `--ipecmd`.
 */
export function ipecmdPath(): string {
  return config().get<string>('ipecmdPath', '').trim();
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
    // detached on POSIX makes the child its own process-group leader, so a timeout can
    // signal the whole group (Python *and* any IPECMD child it spawned) rather than only
    // the Python parent — otherwise the programmer process keeps running after a "timeout".
    const detached = process.platform !== 'win32';
    const child = spawn(command, fullArgs, { cwd: root.uri.fsPath, detached });
    let stdout = '';
    let stderr = '';
    let settled = false;
    let timer: NodeJS.Timeout | undefined;
    let killTimer: NodeJS.Timeout | undefined;
    let timedOut = false;
    const timeoutError = (): Error =>
      new Error(`CC5X command timed out after ${timeoutMs / 1000}s: ${command} ${fullArgs.join(' ')}`);
    // Settle exactly once and always clear the timers / kill a runaway child, so a hung
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
      if (killTimer) {
        clearTimeout(killTimer);
      }
      action();
    };
    // Kill the child and (on POSIX) its whole process group, so a child it spawned (IPECMD)
    // is torn down too. Negative pid targets the group; fall back to the direct child kill.
    const killTree = (signal: NodeJS.Signals): void => {
      if (detached && typeof child.pid === 'number') {
        try {
          process.kill(-child.pid, signal);
          return;
        } catch {
          // The group may already be gone; fall through to a direct kill.
        }
      }
      child.kill(signal);
    };
    // Grace period between the polite SIGTERM and the forced SIGKILL on timeout.
    const KILL_GRACE_MS = 2000;
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        timedOut = true;
        // Politely ask the child (group) to exit; if it ignores SIGTERM, escalate to
        // SIGKILL, then force-settle so a truly unkillable child can't hang the promise.
        killTree('SIGTERM');
        killTimer = setTimeout(() => {
          killTree('SIGKILL');
          killTimer = setTimeout(() => finish(() => reject(timeoutError())), KILL_GRACE_MS);
        }, KILL_GRACE_MS);
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
    // After a timeout the child is being killed; once it actually exits, reject with the
    // timeout error (not a misleading successful/failed result from the killed process).
    child.on('close', (code) =>
      finish(() => (timedOut ? reject(timeoutError()) : resolve({ code: code ?? -1, stdout, stderr }))),
    );
  });
}

/**
 * Run a helper subcommand with `--json` and parse stdout as JSON.
 *
 * By default a `{ok:false, error:{kind,message}}` error-contract payload is thrown (so a
 * failed command surfaces the real error instead of being cast to `T` — otherwise an array
 * consumer later throws "parsed.map is not a function", audit #3). Callers whose result type
 * legitimately carries `ok:false` as a handled outcome (e.g. `generateHeader`/
 * `refreshIntellisense`, where a non-generated-mode project is a normal `{ok:false}` result)
 * pass `rejectError:false` to receive the object instead.
 */
export async function runHelperJson<T>(
  root: vscode.WorkspaceFolder,
  args: string[],
  opts: { rejectError?: boolean } = {},
): Promise<T> {
  const { rejectError = true } = opts;
  const result = await runHelper(root, [...args, '--json']);
  let parsed: unknown;
  try {
    parsed = JSON.parse(result.stdout);
  } catch (err) {
    throw new Error(
      `CC5X helper did not return valid JSON for "${args.join(' ')}" ` +
        `(exit ${result.code}): ${result.stderr || result.stdout}`,
    );
  }
  if (
    rejectError &&
    parsed !== null &&
    typeof parsed === 'object' &&
    !Array.isArray(parsed) &&
    (parsed as { ok?: unknown }).ok === false
  ) {
    const error = (parsed as { error?: { kind?: string; message?: string } }).error;
    throw new Error(
      `CC5X helper "${args.join(' ')}" failed` +
        (error?.kind ? ` [${error.kind}]` : '') +
        (error?.message ? `: ${error.message}` : '') +
        ` (exit ${result.code})`,
    );
  }
  return parsed as T;
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

/**
 * Regenerate the managed config block in the project's `config_source` from the edition's
 * stored config + pack defaults, via `sync-config --project ... --edition ...`. The helper
 * rewrites the file on disk (replacing the existing `// START_CONFIG_SETCC_*.` block or
 * appending one), so callers should save any unsaved edits to that file first.
 */
export function syncProjectConfig(
  root: vscode.WorkspaceFolder,
  edition: string,
): Promise<HelperResult> {
  return runHelper(root, [
    'sync-config',
    '--project',
    manifestAbsPath(root),
    '--edition',
    edition,
  ]);
}

/** Result of `project-generate-header --json`. */
export interface GenerateHeaderResult {
  ok: boolean;
  mode?: string;
  device?: string;
  header?: string;
  error?: { kind: string; message: string };
}

/**
 * Synthesize the device header for a generated-mode project and write it to `header.path`,
 * via `project-generate-header --json`. Reuses the same logic `build` uses, so the file is
 * identical to what a build would produce. Returns `{ok:false, error}` for non-generated
 * projects (which use a provided header) or on failure.
 */
export function generateHeader(
  root: vscode.WorkspaceFolder,
): Promise<GenerateHeaderResult> {
  // {ok:false} (e.g. a supplied/existing-mode project: nothing to synthesize) is a handled
  // result the caller reports via result.error — keep it, do not throw.
  return runHelperJson<GenerateHeaderResult>(
    root,
    ['project-generate-header', '--project', manifestAbsPath(root)],
    { rejectError: false },
  );
}

export interface IntellisenseResult {
  ok: boolean;
  device?: string;
  shim?: string;
  compile_commands?: string;
  defines?: Record<string, string>;
  sources?: string[];
  error?: { kind: string; message: string };
}

/**
 * Generate the editor-only IntelliSense shim + `compile_commands.json` for the project,
 * via `intellisense --json`. These quiet CC5X-dialect false positives in the editor; the
 * CC5X build remains authoritative. Returns `{ok:false, error}` on failure (e.g. a device
 * whose pack metadata cannot be resolved).
 */
export function refreshIntellisense(
  root: vscode.WorkspaceFolder,
): Promise<IntellisenseResult> {
  // {ok:false} (e.g. a device whose pack metadata cannot be resolved) is a handled result
  // the caller reports via result.error — keep it, do not throw.
  return runHelperJson<IntellisenseResult>(
    root,
    ['intellisense', '--project', manifestAbsPath(root)],
    { rejectError: false },
  );
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
