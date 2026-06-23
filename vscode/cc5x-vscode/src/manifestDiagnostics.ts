import * as vscode from 'vscode';
import * as fs from 'fs';
import { manifestAbsPath, runHelperJson } from './helper';

/**
 * One structured, locatable diagnostic from `project-validate --json`.
 *
 * The Python helper owns the actual validation (unknown config symbols, illegal config
 * values, a missing provided header); this module only maps each entry to a range in the
 * manifest text so it shows up in the Problems panel. `edition`+`symbol` locate a config
 * entry; `field` (dotted, e.g. `header.path`) locates a top-level field.
 */
interface ManifestDiagnostic {
  severity: 'error' | 'warning';
  kind: string;
  message: string;
  edition?: string;
  symbol?: string;
  field?: string;
}

interface ValidateResult {
  schema_errors?: string[];
  build_errors?: string[];
  errors?: string[];
  diagnostics?: ManifestDiagnostic[];
}

// Monotonic guard so a slow validate racing a newer manifest change cannot publish stale
// diagnostics over fresh ones (the manifest can change several times in quick succession).
let generation = 0;

/**
 * Run `project-validate --json` for the workspace manifest and publish its structured
 * diagnostics to `collection`, anchored to the offending line in `setcc-native.json`.
 *
 * Best-effort and self-clearing: if the helper cannot produce JSON (e.g. the manifest is
 * syntactically broken — the JSON language service reports that itself) the collection is
 * cleared rather than left stale.
 */
export async function publishManifestDiagnostics(
  root: vscode.WorkspaceFolder,
  collection: vscode.DiagnosticCollection,
): Promise<void> {
  const myGen = ++generation;
  const manifestPath = manifestAbsPath(root);
  const uri = vscode.Uri.file(manifestPath);

  let result: ValidateResult;
  try {
    result = await runHelperJson<ValidateResult>(root, ['project-validate', '--project', manifestPath]);
  } catch {
    // The helper produced no JSON — a transient cause (timeout, missing interpreter) or a
    // syntactically broken manifest (the JSON language service flags that itself). Preserve
    // the last good diagnostics rather than wiping them to a misleading "all clear"; the next
    // successful run refreshes them. Deletion happens only on a clean 0-diagnostic run or an
    // explicit manifest delete (clearManifestDiagnostics).
    return;
  }
  // A newer run started while we awaited — let it own the result.
  if (myGen !== generation) {
    return;
  }

  const diags = collectValidateDiagnostics(result);
  if (diags.length === 0) {
    collection.delete(uri);
    return;
  }

  let text = '';
  try {
    text = fs.readFileSync(manifestPath, 'utf8');
  } catch {
    // Manifest unreadable — ranges fall back to the file head; still surface the messages.
  }
  collection.set(
    uri,
    diags.map((d) => toDiagnostic(d, text)),
  );
}

function collectValidateDiagnostics(result: ValidateResult): ManifestDiagnostic[] {
  const diagnostics = [...(result.diagnostics ?? [])];
  const seenMessages = new Set(diagnostics.map((d) => d.message));
  const addMessages = (kind: string, messages: string[] | undefined): void => {
    for (const message of messages ?? []) {
      if (seenMessages.has(message)) {
        continue;
      }
      seenMessages.add(message);
      diagnostics.push({ severity: 'error', kind, message });
    }
  };
  // These helper fields are not locatable today, so they intentionally anchor to the file
  // head instead of being dropped from the Problems panel.
  addMessages('schema_error', result.schema_errors);
  addMessages('build_error', result.build_errors);
  addMessages('validation_error', result.errors);
  return diagnostics;
}

/**
 * Invalidate any in-flight validate and clear the manifest diagnostics — use on manifest
 * delete. Bumping `generation` is essential: an `onDidChange`-triggered validate may be
 * awaiting the helper when the file is deleted, and without this bump it would pass its
 * post-await guard and re-publish stale diagnostics for the now-gone file.
 */
export function clearManifestDiagnostics(collection: vscode.DiagnosticCollection): void {
  generation++;
  collection.clear();
}

function toDiagnostic(d: ManifestDiagnostic, text: string): vscode.Diagnostic {
  const severity =
    d.severity === 'warning'
      ? vscode.DiagnosticSeverity.Warning
      : vscode.DiagnosticSeverity.Error;
  const diagnostic = new vscode.Diagnostic(locateRange(d, text), d.message, severity);
  diagnostic.source = 'CC5X';
  diagnostic.code = d.kind;
  return diagnostic;
}

/** Anchor a diagnostic to the manifest key it concerns, or the file head if unlocatable. */
function locateRange(d: ManifestDiagnostic, text: string): vscode.Range {
  const segments = keyPath(d);
  const found = segments && text ? locateKey(text, segments) : undefined;
  if (!found) {
    return new vscode.Range(0, 0, 0, 0);
  }
  return new vscode.Range(
    offsetToPosition(text, found.offset),
    offsetToPosition(text, found.offset + found.length),
  );
}

/** JSON key path to highlight: a dotted `field`, or the edition's config symbol entry. */
function keyPath(d: ManifestDiagnostic): string[] | undefined {
  if (d.field) {
    return d.field.split('.');
  }
  if (d.edition && d.symbol) {
    return ['editions', d.edition, 'config', d.symbol];
  }
  return undefined;
}

/**
 * Walk a sequence of quoted JSON keys, each searched for after the previous match, and
 * return the span of the final key token. A pragmatic text scan (not a full JSON parse):
 * manifests are small and machine-written with sorted keys, so the first `"key"` after the
 * parent key is the right one. Returns undefined (→ file-head fallback) if any key is absent.
 */
function locateKey(text: string, segments: string[]): { offset: number; length: number } | undefined {
  let from = 0;
  let last: { offset: number; length: number } | undefined;
  for (const segment of segments) {
    const needle = `"${segment}"`;
    const index = text.indexOf(needle, from);
    if (index === -1) {
      return undefined;
    }
    last = { offset: index, length: needle.length };
    from = index + needle.length;
  }
  return last;
}

function offsetToPosition(text: string, offset: number): vscode.Position {
  let line = 0;
  let lineStart = 0;
  for (let i = 0; i < offset; i++) {
    if (text.charCodeAt(i) === 10 /* \n */) {
      line++;
      lineStart = i + 1;
    }
  }
  return new vscode.Position(line, offset - lineStart);
}
