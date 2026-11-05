/**
 * Shared helpers for the golden (integration) Node tests.
 *
 * Golden tests spawn the real Python CLI offline. Everything they need is
 * discovered at run time and each test *skips explicitly* (with a reason) when
 * the Python environment or a command is not available, so a contributor
 * without the optional Python setup still gets a green Node-only run.
 */

import { spawnSync } from "node:child_process";
import { join } from "node:path";

import { loadDist, processEnv, repoRoot, venvPython } from "../unit/_helpers.mjs";

export { loadDist, processEnv, repoRoot, venvPython };

/** Skip reason when the repository venv is missing, or `false` when Python is usable. */
export const pythonSkipReason = venvPython()
  ? false
  : "no .venv/bin/python in this checkout — run the Python setup (see docs/install.md) to enable golden tests";

/** Environment for spawning the Python CLI directly. */
export function pythonEnv() {
  const env = { ...processEnv() };
  const parts = [join(repoRoot, "python")];
  if (typeof process.env.PYTHONPATH === "string" && process.env.PYTHONPATH.trim().length > 0) {
    parts.push(process.env.PYTHONPATH);
  }
  env.PYTHONPATH = parts.join(process.platform === "win32" ? ";" : ":");
  if (!env.PYTHONIOENCODING) env.PYTHONIOENCODING = "utf-8";
  return env;
}

/**
 * Run `python -m lifesci <args>` and capture the result.
 * @param {readonly string[]} args - arguments after `-m lifesci`.
 * @param {{cwd?: string, timeout?: number}} [options]
 */
export function runLifesciCli(args, options = {}) {
  const python = venvPython();
  if (!python) throw new Error("runLifesciCli called without a Python interpreter");
  return spawnSync(python, ["-m", "lifesci", ...args], {
    cwd: options.cwd ?? repoRoot,
    env: pythonEnv(),
    encoding: "utf8",
    timeout: options.timeout ?? 180_000,
  });
}

/** Run `python -m lifesci.<module> <args>` (e.g. `lifesci.toolspec --check`). */
export function runLifesciModule(moduleName, args, options = {}) {
  const python = venvPython();
  if (!python) throw new Error("runLifesciModule called without a Python interpreter");
  return spawnSync(python, ["-m", moduleName, ...args], {
    cwd: options.cwd ?? repoRoot,
    env: pythonEnv(),
    encoding: "utf8",
    timeout: options.timeout ?? 120_000,
  });
}

/** Load the compiled bridge/config/tool-spec modules used by the golden tests. */
export async function loadPluginModules() {
  const [bridge, config, specsModule] = await Promise.all([
    loadDist("bridge.js"),
    loadDist("config.js"),
    loadDist("tool-specs.js"),
  ]);
  return { bridge, config, specs: specsModule.TOOL_SPECS };
}

/** Find the generated spec for a dotted command name, or undefined. */
export function findToolSpec(specs, command) {
  return specs.tools.find((spec) => spec.command === command);
}
