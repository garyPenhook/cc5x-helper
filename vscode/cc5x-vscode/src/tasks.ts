import * as vscode from 'vscode';
import { helperPath, manifestAbsPath, pythonPath } from './helper';

interface Cc5xTaskDefinition extends vscode.TaskDefinition {
  action: 'build';
  edition: string;
  project?: string;
}

const TASK_SOURCE = 'CC5X';

/** Build a `ProcessExecution` that runs `helper build --project <m> --edition <e>`. */
function buildExecution(root: vscode.WorkspaceFolder, edition: string): vscode.ProcessExecution {
  return new vscode.ProcessExecution(
    pythonPath(),
    [helperPath(root), 'build', '--project', manifestAbsPath(root), '--edition', edition],
    { cwd: root.uri.fsPath },
  );
}

/**
 * Resolves user-authored `type: cc5x` tasks from tasks.json.
 *
 * It deliberately does NOT enumerate a `Build <edition>` task per edition: the
 * `CC5X: Generate VS Code Tasks` command writes concrete `CC5X: Build …` (plus Program/
 * Verify/Erase) entries into tasks.json, which is the single source of runnable tasks.
 * Contributing them here as well produced two "CC5X: Build X" entries in the task list
 * (audit A5). The `CC5X: Build` command palette entry still covers zero-config one-off
 * builds without a tasks.json.
 */
export class Cc5xTaskProvider implements vscode.TaskProvider {
  constructor(private readonly root: vscode.WorkspaceFolder) {}

  provideTasks(): vscode.Task[] {
    return [];
  }

  resolveTask(task: vscode.Task): vscode.Task | undefined {
    const definition = task.definition as Cc5xTaskDefinition;
    if (definition.action !== 'build' || !definition.edition) {
      return undefined;
    }
    // resolveTask must reuse the same definition object.
    const resolved = new vscode.Task(
      definition,
      task.scope ?? this.root,
      `Build ${definition.edition}`,
      TASK_SOURCE,
      buildExecution(this.root, definition.edition),
      '$cc5x',
    );
    resolved.group = vscode.TaskGroup.Build;
    return resolved;
  }
}
