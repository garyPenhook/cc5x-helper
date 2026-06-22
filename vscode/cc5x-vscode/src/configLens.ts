import * as vscode from 'vscode';
import * as path from 'path';
import { ProjectInfo, resolveManifestRelative } from './helper';

// The helper delimits the managed config block with these markers (5x = PIC10/12/16,
// 8e = PIC18). Mirrors START_MARKERS in tools/cc5x_setcc_native.py.
const START_MARKERS = ['START_CONFIG_SETCC_5X.', 'START_CONFIG_SETCC_8E.'];

/**
 * Shows a "CC5X: Sync Config" CodeLens over the managed config block in the project's
 * config source. Clicking it runs `sync-config` to regenerate the block from the edition's
 * stored config + pack defaults.
 *
 * The lens is offered only in the project's `config_source` file, because
 * `sync-config --project` rewrites that file regardless of which editor is active — showing
 * it elsewhere would mislead the user about what "Sync" touches.
 */
export class Cc5xConfigLensProvider implements vscode.CodeLensProvider {
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.emitter.event;

  constructor(
    private readonly getRoot: () => vscode.WorkspaceFolder | undefined,
    private readonly getProject: () => ProjectInfo | undefined,
  ) {}

  /** Re-evaluate lenses; call when the manifest/project changes (config_source may move). */
  refresh(): void {
    this.emitter.fire();
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    // Only real on-disk files: skip diff/peek/git views that share the same fsPath but
    // are read-only renderings where a "Sync" action would be misleading.
    if (document.uri.scheme !== 'file') {
      return [];
    }
    const root = this.getRoot();
    const project = this.getProject();
    if (!root || !project) {
      return [];
    }
    const configSource = resolveManifestRelative(root, project.config_source);
    if (path.normalize(document.uri.fsPath) !== path.normalize(configSource)) {
      return [];
    }
    for (let line = 0; line < document.lineCount; line++) {
      const text = document.lineAt(line).text;
      if (START_MARKERS.some((marker) => text.includes(marker))) {
        const range = new vscode.Range(line, 0, line, text.length);
        return [
          new vscode.CodeLens(range, {
            title: '$(sync) CC5X: Sync Config',
            command: 'cc5x.syncConfig',
            arguments: [document.uri],
          }),
        ];
      }
    }
    return [];
  }
}
