/**
 * Shared helpers for the Node unit tests.
 *
 * The tests import the *compiled* plugin (`dist/`) — the same artifacts the
 * harness loads — so they catch packaging/emit problems a source-only test
 * would miss. The runner (`scripts/run-tests.mjs`) builds before testing; when
 * run directly, `loadDist` explains how to fix a missing build.
 */

import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

/** Repository root (`tests/unit/_helpers.mjs` → `<repo>`). */
export const repoRoot = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

/** Compiled plugin directory. */
export const distDir = join(repoRoot, "dist");

/**
 * Import one compiled module from `dist/`.
 * @param {string} relative - path below `dist/`, e.g. `"bridge.js"`.
 * @returns {Promise<any>} the module namespace.
 */
export async function loadDist(relative) {
  const path = join(distDir, relative);
  if (!existsSync(path)) {
    throw new Error(
      `dist/${relative} is missing. Run \`npm run build\` before the Node tests ` +
        "(they import the compiled plugin, exactly what the harness loads).",
    );
  }
  return import(pathToFileURL(path).href);
}

/** Create a temp directory; registers cleanup on the node:test context when given. */
export function makeTempDir(t, prefix = "lifesci-unit-") {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  if (t) t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

/** A complete child environment as `Record<string, string>`. */
export function processEnv() {
  return Object.fromEntries(
    Object.entries(process.env).filter(([, value]) => typeof value === "string"),
  );
}

/** The venv interpreter used by the Python layer, or null when absent. */
export function venvPython() {
  const candidates = [
    process.env.LIFESCI_PYTHON,
    join(repoRoot, ".venv", "bin", "python"),
    join(repoRoot, ".venv", "Scripts", "python.exe"),
  ].filter((value) => typeof value === "string" && value.length > 0);
  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

/** Environment for spawning the Python CLI directly. */
export function pythonEnv() {
  const env = { ...processEnv() };
  const pythonPath = [join(repoRoot, "python")];
  if (typeof process.env.PYTHONPATH === "string" && process.env.PYTHONPATH.trim().length > 0) {
    pythonPath.push(process.env.PYTHONPATH);
  }
  env.PYTHONPATH = pythonPath.join(process.platform === "win32" ? ";" : ":");
  if (!env.PYTHONIOENCODING) env.PYTHONIOENCODING = "utf-8";
  return env;
}
