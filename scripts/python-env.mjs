/**
 * Shared Python-environment discovery for the build/test scripts.
 *
 * The plugin's science layer lives in `python/lifesci`; the build and the
 * offline test runner need the same interpreter priority as the runtime bridge:
 *
 *   1. explicit `--python` argument (or `LIFESCI_PYTHON`),
 *   2. `$LIFESCI_PYTHON`,
 *   3. the checkout venv `<repo>/.venv/bin/python`,
 *   4. `python3` on `PATH`, then `python`.
 *
 * This file is build tooling only (never shipped to the agent as plugin code).
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { delimiter, dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Package root, resolved from a script URL under `<repo>/scripts/`. */
export function repoRootFromScript(scriptUrl) {
  return dirname(dirname(fileURLToPath(scriptUrl)));
}

/** Directories prepended to `PYTHONPATH` for every Python invocation. */
export function pythonSearchPath(repoRoot) {
  return [join(repoRoot, "python")];
}

/** Build the child environment: inherited env + `PYTHONPATH=<repo>/python[:...]` + UTF-8 stdio. */
export function pythonEnv(repoRoot, extra = {}) {
  const env = { ...process.env, ...extra };
  const parts = [...pythonSearchPath(repoRoot)];
  if (typeof process.env.PYTHONPATH === "string" && process.env.PYTHONPATH.trim().length > 0) {
    parts.push(process.env.PYTHONPATH);
  }
  env.PYTHONPATH = parts.join(delimiter);
  if (!env.PYTHONIOENCODING) env.PYTHONIOENCODING = "utf-8";
  return env;
}

/** Candidate interpreter list in priority order, de-duplicated. */
export function pythonCandidates(repoRoot, override) {
  const candidates = [];
  if (typeof override === "string" && override.trim().length > 0) candidates.push(override.trim());
  if (typeof process.env.LIFESCI_PYTHON === "string" && process.env.LIFESCI_PYTHON.trim().length > 0) {
    candidates.push(process.env.LIFESCI_PYTHON.trim());
  }
  const venv =
    process.platform === "win32"
      ? join(repoRoot, ".venv", "Scripts", "python.exe")
      : join(repoRoot, ".venv", "bin", "python");
  candidates.push(venv);
  candidates.push(process.platform === "win32" ? "python" : "python3");
  candidates.push("python");
  return [...new Set(candidates)];
}

/**
 * Probe one interpreter: it must spawn and report a version.
 * @returns `{ok: true, version}` or `{ok: false, detail}`.
 */
export function probePython(candidate, repoRoot, env = pythonEnv(repoRoot)) {
  if (isAbsolute(candidate) && !existsSync(candidate)) {
    return { ok: false, detail: "interpreter not found on disk" };
  }
  const result = spawnSync(candidate, ["-c", "import sys; print(sys.version.split()[0])"], {
    cwd: repoRoot,
    env,
    encoding: "utf8",
    timeout: 30_000,
  });
  if (result.error) {
    return { ok: false, detail: result.error.message };
  }
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim();
    return { ok: false, detail: detail.length > 0 ? detail : `exit code ${result.status}` };
  }
  return { ok: true, version: (result.stdout || "").trim() };
}

/**
 * Find the first usable Python interpreter.
 *
 * @param repoRoot - repository root.
 * @param options - `{override?: string}` explicit `--python` value.
 * @returns `{python, version, env}` or `{python: null, attempts}` when none works.
 */
export function findPython(repoRoot, options = {}) {
  const env = pythonEnv(repoRoot);
  const attempts = [];
  for (const candidate of pythonCandidates(repoRoot, options.override)) {
    const probe = probePython(candidate, repoRoot, env);
    attempts.push({ candidate, ...probe });
    if (probe.ok) return { python: candidate, version: probe.version, env, attempts };
  }
  return { python: null, env, attempts };
}

/** Print the attempted interpreters so a missing/broken venv is easy to diagnose. */
export function describeAttempts(attempts) {
  return attempts
    .map((attempt) => {
      const label = attempt.ok ? `ok (Python ${attempt.version})` : `failed: ${attempt.detail}`;
      return `  - ${attempt.candidate}: ${label}`;
    })
    .join("\n");
}

/** Actionable setup hint shared by the scripts. */
export function pythonSetupHint(repoRoot) {
  return (
    `Create the Python environment first:\n` +
    `  cd ${repoRoot}\n` +
    `  python3 -m venv .venv\n` +
    `  .venv/bin/pip install -r requirements.txt -r requirements-dev.txt\n` +
    `or point LIFESCI_PYTHON / --python at an existing Python >= 3.11 interpreter.`
  );
}
