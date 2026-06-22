import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

// CC5X emits diagnostics as: "Error <file> <line>: <message>" / "Warning <file> <line>: <message>".
// There is no column. Confirmed against real CC5X 3.8C output (e.g. the 16F1509.H header bug).
// The `:\s+` (not `:\s*`) keeps this in lockstep with the contributed `$cc5x` problem
// matcher in package.json so both agree on empty-message lines (audit A-minor).
const CC5X_DIAGNOSTIC_RE = /^(Error|Warning)\s+(.+?)\s+(\d+):\s+(.*)$/;

/**
 * Parse CC5X build output and populate the diagnostic collection.
 *
 * @param output  combined stdout+stderr from the build.
 * @param baseDir directory the compiler ran in; bare filenames resolve against it.
 */
export function publishCc5xDiagnostics(
  output: string,
  baseDir: string,
  collection: vscode.DiagnosticCollection,
): number {
  collection.clear();
  const byFile = new Map<string, vscode.Diagnostic[]>();

  for (const rawLine of output.split(/\r?\n/)) {
    const match = CC5X_DIAGNOSTIC_RE.exec(rawLine.trim());
    if (!match) {
      continue;
    }
    const [, severityWord, file, lineText, message] = match;
    const line = Math.max(0, parseInt(lineText, 10) - 1); // CC5X is 1-based; VS Code 0-based.
    const resolved = resolveFile(file, baseDir);

    // No column info: flag the whole line.
    const range = new vscode.Range(line, 0, line, Number.MAX_SAFE_INTEGER);
    const severity =
      severityWord.toLowerCase() === 'error'
        ? vscode.DiagnosticSeverity.Error
        : vscode.DiagnosticSeverity.Warning;
    const diagnostic = new vscode.Diagnostic(range, message, severity);
    diagnostic.source = 'CC5X';

    const list = byFile.get(resolved) ?? [];
    list.push(diagnostic);
    byFile.set(resolved, list);
  }

  let total = 0;
  for (const [file, diags] of byFile) {
    collection.set(vscode.Uri.file(file), diags);
    total += diags.length;
  }
  return total;
}

/** Resolve a CC5X-reported filename (often bare) to an absolute path. */
function resolveFile(file: string, baseDir: string): string {
  if (path.isAbsolute(file)) {
    return file;
  }
  const candidate = path.join(baseDir, file);
  if (fs.existsSync(candidate)) {
    return candidate;
  }
  // CC5X may report a header by name only; try a case-insensitive match in baseDir.
  try {
    const lower = file.toLowerCase();
    for (const entry of fs.readdirSync(baseDir)) {
      if (entry.toLowerCase() === lower) {
        return path.join(baseDir, entry);
      }
    }
  } catch {
    // baseDir unreadable — fall through to the naive join.
  }
  return candidate;
}
