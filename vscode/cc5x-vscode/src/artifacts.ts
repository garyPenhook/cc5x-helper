import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

// CC5X build outputs worth surfacing (see design doc).
const ARTIFACT_EXTENSIONS = ['.hex', '.asm', '.occ', '.var', '.fcs', '.cpr', '.cod', '.cof'];

export class ArtifactItem extends vscode.TreeItem {
  constructor(public readonly filePath: string) {
    super(path.basename(filePath), vscode.TreeItemCollapsibleState.None);
    this.resourceUri = vscode.Uri.file(filePath);
    this.description = path.extname(filePath).slice(1).toUpperCase();
    this.command = {
      command: 'cc5x.openArtifact',
      title: 'Open Artifact',
      arguments: [this.resourceUri],
    };
    this.contextValue = 'cc5xArtifact';
  }
}

/**
 * Tree of CC5X build artifacts. Scans the directories that builds write into
 * (the source directory and any `build/` subtree under the workspace).
 */
export class ArtifactsProvider implements vscode.TreeDataProvider<ArtifactItem> {
  private readonly emitter = new vscode.EventEmitter<ArtifactItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  private searchDirs: string[] = [];
  private cache: ArtifactItem[] | undefined;
  // Bumped on every refresh; a scan only writes the cache if its generation is still
  // current, so a slow scan started before a refresh cannot overwrite newer results.
  private generation = 0;

  setSearchDirs(dirs: string[]): void {
    this.searchDirs = dirs;
    this.refresh();
  }

  refresh(): void {
    this.cache = undefined; // invalidate; the next getChildren rescans off the UI thread
    this.generation++;
    this.emitter.fire(undefined);
  }

  getTreeItem(element: ArtifactItem): vscode.TreeItem {
    return element;
  }

  // Async so the directory walk does not block the extension host UI thread (audit A10);
  // the result is cached until the next refresh()/setSearchDirs().
  async getChildren(element?: ArtifactItem): Promise<ArtifactItem[]> {
    if (element) {
      return [];
    }
    if (this.cache) {
      return this.cache;
    }
    const gen = this.generation;
    const found = new Map<string, number>(); // path -> mtime, for de-dup + sorting
    for (const dir of this.searchDirs) {
      await this.collect(dir, found);
    }
    const items = [...found.entries()]
      .sort((a, b) => b[1] - a[1]) // newest first
      .map(([file]) => new ArtifactItem(file));
    // Only cache if no refresh raced ahead of this scan (audit follow-up); otherwise a
    // stale walk could clobber the newer scan's results and hide just-built artifacts.
    if (gen === this.generation) {
      this.cache = items;
    }
    return items;
  }

  private async collect(dir: string, found: Map<string, number>, depth = 0): Promise<void> {
    if (depth > 4) {
      return;
    }
    let entries: fs.Dirent[];
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== 'node_modules' && !entry.name.startsWith('.')) {
          await this.collect(full, found, depth + 1);
        }
      } else if (ARTIFACT_EXTENSIONS.includes(path.extname(entry.name).toLowerCase())) {
        try {
          found.set(full, (await fs.promises.stat(full)).mtimeMs);
        } catch {
          found.set(full, 0);
        }
      }
    }
  }
}
