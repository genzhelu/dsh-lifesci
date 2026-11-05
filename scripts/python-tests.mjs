/**
 * Run the offline Python test suites (`python/tests/unit` + `python/tests/golden`).
 *
 * `npm test` calls this after the Node unit/golden tests. It is tolerant of a
 * work-in-progress test tree:
 *
 *  - no `python/tests` directories yet   → skip with a notice (exit 0)
 *  - pytest not installed                → skip with a notice (exit 0)
 *  - pytest exit code 5 (nothing collected) → pass with a notice
 *  - real test failures                  → exit with pytest's status
 *
 * Usage:
 *   node scripts/python-tests.mjs                  # offline unit + golden
 *   node scripts/python-tests.mjs --network        # network-marked tests from unit/golden/network
 *   node scripts/python-tests.mjs --python /path   # explicit interpreter
 *   node scripts/python-tests.mjs -- -k doctor     # extra pytest arguments
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join, relative } from "node:path";

import { describeAttempts, findPython, pythonEnv, pythonSetupHint, repoRootFromScript } from "./python-env.mjs";

const repoRoot = repoRootFromScript(import.meta.url);

function usage() {
  process.stdout.write(
    [
      "Usage: node scripts/python-tests.mjs [options] [-- extra pytest args]",
      "",
      "Options:",
      "  --network       run network-marked tests (unit + golden + network dirs)",
      "  --python <cmd>  Python interpreter (default: $LIFESCI_PYTHON, .venv/bin/python, python3)",
      "  -h, --help      show this help",
      "",
    ].join("\n"),
  );
}

function parseArgs(argv) {
  const options = { network: false, python: undefined, help: false, extra: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--") {
      options.extra = argv.slice(index + 1);
      break;
    }
    if (arg === "--network") {
      options.network = true;
    } else if (arg === "--python") {
      const value = argv[index + 1];
      if (value === undefined) throw new Error("--python requires a value");
      options.python = value;
      index += 1;
    } else if (arg === "-h" || arg === "--help") {
      options.help = true;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return options;
}

function missingPytest(output) {
  return /No module named ['"]?pytest['"]?/i.test(output);
}

function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`lifesci python tests: ${error.message}\n`);
    usage();
    return 2;
  }
  if (options.help) {
    usage();
    return 0;
  }

  // Offline runs cover BOTH suites (unit + golden); network runs select the
  // network-marked tests wherever they live (unit, golden, and the dedicated
  // python/tests/network directory).
  const testRoot = join(repoRoot, "python", "tests");
  const candidates = options.network
    ? [join(testRoot, "unit"), join(testRoot, "golden"), join(testRoot, "network")]
    : [join(testRoot, "unit"), join(testRoot, "golden")];
  const existing = candidates.filter((directory) => existsSync(directory));
  if (!options.network && existing.length > 0 && !existing.includes(join(testRoot, "golden"))) {
    process.stdout.write(
      "\n[lifesci python tests] notice: python/tests/golden does not exist — running the unit suite only.\n",
    );
  }
  if (existing.length === 0) {
    process.stdout.write(
      `\n[lifesci python tests] skipped: ${candidates
        .map((directory) => relative(repoRoot, directory))
        .join(", ")} do not exist.\n`,
    );
    return 0;
  }

  const found = findPython(repoRoot, { override: options.python });
  if (found.python === null) {
    process.stderr.write(
      "\n[lifesci python tests] skipped: no usable Python interpreter found; Python tests not run.\n" +
        `Tried:\n${describeAttempts(found.attempts)}\n\n${pythonSetupHint(repoRoot)}\n`,
    );
    // A missing local venv is a skip, not a failure: node-only checks still ran.
    return 0;
  }

  const pytestArgs = [
    "-m",
    "pytest",
    "-p",
    "no:cacheprovider",
    ...existing,
    "-m",
    options.network ? "network" : "not network",
    ...options.extra,
  ];
  const env = pythonEnv(repoRoot, options.network ? { LIFESCI_NETWORK: "1" } : {});
  process.stdout.write(
    `\n[lifesci python tests] ${found.python} ${pytestArgs.join(" ")}\n` +
      `  (cwd ${repoRoot}, PYTHONPATH=${env.PYTHONPATH})\n\n`,
  );
  const result = spawnSync(found.python, pytestArgs, {
    cwd: repoRoot,
    env,
    encoding: "utf8",
    timeout: options.network ? 30 * 60_000 : 10 * 60_000,
  });

  const stdout = result.stdout || "";
  const stderr = result.stderr || "";
  if (stdout.length > 0) process.stdout.write(stdout);
  if (stderr.length > 0) process.stderr.write(stderr);

  if (result.error) {
    process.stderr.write(`\n[lifesci python tests] could not run pytest: ${result.error.message}\n`);
    return 1;
  }
  if (missingPytest(`${stdout}\n${stderr}`)) {
    process.stderr.write(
      "\n[lifesci python tests] skipped: pytest is not installed in this interpreter " +
        "(install requirements-dev.txt to enable the offline Python suites).\n",
    );
    return 0;
  }
  switch (result.status) {
    case 0:
      process.stdout.write("\n[lifesci python tests] passed.\n");
      return 0;
    case 5:
      process.stdout.write("\n[lifesci python tests] no tests collected (pytest exit code 5) — treated as pass.\n");
      return 0;
    case null:
      process.stderr.write("\n[lifesci python tests] pytest was killed (timeout or signal).\n");
      return 1;
    default:
      process.stderr.write(`\n[lifesci python tests] failed with pytest exit code ${result.status}.\n`);
      return result.status;
  }
}

process.exitCode = main();
