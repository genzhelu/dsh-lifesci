/**
 * `npm test` entry point: build, Node unit/golden tests, offline Python tests.
 *
 * Steps (fail-fast except the Python suite, which reports its own status):
 *
 *   1. generate `src/tool-specs.ts` from the Python command registry;
 *   2. typecheck + emit `dist/` with `tsc -p tsconfig.json`;
 *   3. `node --test "tests/unit/*.test.mjs" "tests/golden/*.test.mjs"`
 *      (no network, no Python required);
 *   4. `node scripts/python-tests.mjs` (offline pytest, skips gracefully while the
 *      Python test tree is still being written).
 *
 * Usage:
 *   node scripts/run-tests.mjs             # full offline suite
 *   node scripts/run-tests.mjs --no-python # Node build + tests only
 *   node scripts/run-tests.mjs -h
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { repoRootFromScript } from "./python-env.mjs";

const repoRoot = repoRootFromScript(import.meta.url);
const nodeBin = process.execPath;

function usage() {
  process.stdout.write(
    [
      "Usage: node scripts/run-tests.mjs [options]",
      "",
      "Options:",
      "  --no-python   skip the offline Python pytest suite",
      "  -h, --help    show this help",
      "",
    ].join("\n"),
  );
}

function parseArgs(argv) {
  const options = { python: true, help: false };
  for (const arg of argv) {
    if (arg === "--no-python") options.python = false;
    else if (arg === "-h" || arg === "--help") options.help = true;
    else throw new Error(`unknown argument: ${arg}`);
  }
  return options;
}

function header(title) {
  process.stdout.write(`\n=== ${title} ===\n`);
}

function runStep(title, command, args, options = {}) {
  header(title);
  process.stdout.write(`$ ${command} ${args.join(" ")}\n`);
  const startedAt = Date.now();
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    stdio: "inherit",
    env: { ...process.env, ...(options.env ?? {}) },
    timeout: options.timeoutMs ?? 15 * 60_000,
  });
  const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
  if (result.error) {
    process.stderr.write(`\n${title}: failed to start (${seconds}s): ${result.error.message}\n`);
    return { ok: false, status: 1 };
  }
  if (result.status !== 0) {
    process.stderr.write(`\n${title}: FAILED (exit code ${result.status}, ${seconds}s)\n`);
    return { ok: false, status: result.status === null ? 1 : result.status };
  }
  process.stdout.write(`${title}: ok (${seconds}s)\n`);
  return { ok: true, status: 0 };
}

function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`lifesci tests: ${error.message}\n`);
    usage();
    return 2;
  }
  if (options.help) {
    usage();
    return 0;
  }

  const failures = [];
  const tscBin = join(repoRoot, "node_modules", "typescript", "bin", "tsc");
  if (!existsSync(tscBin)) {
    process.stderr.write(
      "lifesci tests: TypeScript is not installed (node_modules/typescript missing).\n" +
        "Install dev dependencies inside the workspace:\n" +
        "  npm install --cache=./.npm-cache --no-audit --no-fund\n",
    );
    return 1;
  }

  // 1. generated tool specs (source of truth: python/lifesci/commands)
  const generate = runStep("generate tool specs", nodeBin, [join(repoRoot, "scripts", "generate-tool-specs.mjs")], {
    timeoutMs: 3 * 60_000,
  });
  if (!generate.ok) return generate.status;

  // 2. compile src/ -> dist/
  const build = runStep("build (tsc)", nodeBin, [tscBin, "-p", join(repoRoot, "tsconfig.json")], {
    timeoutMs: 5 * 60_000,
  });
  if (!build.ok) return build.status;

  // 3. Node unit + golden tests (they import dist/, so build must be first).
  // Glob patterns (not bare directories): Node >= 22.23 treats positional test
  // runner arguments as file globs, so passing a directory would try to load it
  // as a module. The quoted patterns are expanded by the test runner itself.
  const nodeTestPatterns = [join(repoRoot, "tests", "unit"), join(repoRoot, "tests", "golden")]
    .filter((dir) => existsSync(dir))
    .map((dir) => join(dir, "*.test.mjs"));
  if (nodeTestPatterns.length === 0) {
    process.stderr.write("\ntests/unit and tests/golden do not exist; nothing to run.\n");
    return 1;
  }
  const nodeTests = runStep("node tests (unit + golden)", nodeBin, ["--test", ...nodeTestPatterns], {
    timeoutMs: 10 * 60_000,
  });
  if (!nodeTests.ok) failures.push("node tests");

  // 4. offline Python suites
  if (options.python) {
    const pythonTests = runStep("python tests (offline)", nodeBin, [join(repoRoot, "scripts", "python-tests.mjs")], {
      timeoutMs: 15 * 60_000,
    });
    if (!pythonTests.ok) failures.push("python tests");
  } else {
    process.stdout.write("\n=== python tests (offline) ===\nskipped by --no-python\n");
  }

  if (failures.length > 0) {
    process.stderr.write(`\nTEST SUITE FAILED: ${failures.join(", ")}\n`);
    return 1;
  }
  process.stdout.write("\nAll lifesci test suites passed.\n");
  return 0;
}

process.exitCode = main();
