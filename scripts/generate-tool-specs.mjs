/**
 * Generate `src/tool-specs.ts` from the Python command registry.
 *
 * The Python registry is the single source of truth for tool names, input
 * schemas and descriptions. Running this script during `npm run build` makes it
 * impossible for the agent-facing tool schema to drift from the CLI.
 *
 * Usage:
 *   node scripts/generate-tool-specs.mjs                 # write src/tool-specs.ts
 *   node scripts/generate-tool-specs.mjs --check         # CI drift check
 *   node scripts/generate-tool-specs.mjs --python /path  # explicit interpreter
 *   node scripts/generate-tool-specs.mjs --out other.ts
 */

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { relative, resolve } from "node:path";

import { describeAttempts, findPython, pythonEnv, pythonSetupHint, repoRootFromScript } from "./python-env.mjs";

const repoRoot = repoRootFromScript(import.meta.url);

function usage() {
  process.stdout.write(
    [
      "Usage: node scripts/generate-tool-specs.mjs [options]",
      "",
      "Options:",
      "  --out <path>    output file (default: src/tool-specs.ts)",
      "  --check         exit 1 if <out> differs from generated output (no write)",
      "  --python <cmd>  Python interpreter (default: $LIFESCI_PYTHON, .venv/bin/python, python3)",
      "  -h, --help      show this help",
      "",
    ].join("\n"),
  );
}

function parseArgs(argv) {
  const options = { out: "src/tool-specs.ts", check: false, python: undefined, help: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--out" || arg === "--python") {
      const value = argv[index + 1];
      if (value === undefined || value.startsWith("--")) {
        throw new Error(`${arg} requires a value`);
      }
      options[arg.slice(2)] = value;
      index += 1;
    } else if (arg === "--check") {
      options.check = true;
    } else if (arg === "-h" || arg === "--help") {
      options.help = true;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return options;
}

/** Extract the number of tools from the generated TypeScript payload (for the summary line). */
function summarizeGenerated(outPath) {
  try {
    const text = readFileSync(outPath, "utf8");
    const match = /const TOOL_SPECS = ([\s\S]*?) as const;\s*$/.exec(text);
    if (!match) return "unknown";
    const payload = JSON.parse(match[1]);
    const names = Array.isArray(payload.tools) ? payload.tools.map((tool) => tool.command) : [];
    return `${names.length} tool specs (v${payload.version}): ${names.join(", ")}`;
  } catch {
    return "unknown";
  }
}

function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`lifesci.toolspec: ${error.message}\n`);
    usage();
    return 2;
  }
  if (options.help) {
    usage();
    return 0;
  }

  const outPath = resolve(repoRoot, options.out);
  const found = findPython(repoRoot, { override: options.python });
  if (found.python === null) {
    process.stderr.write(
      "lifesci.toolspec: no usable Python interpreter found.\n" +
        `Tried:\n${describeAttempts(found.attempts)}\n\n${pythonSetupHint(repoRoot)}\n`,
    );
    return 1;
  }

  const argv = options.check
    ? ["-m", "lifesci.toolspec", "--check", outPath]
    : ["-m", "lifesci.toolspec", "--out", outPath];

  const result = spawnSync(found.python, argv, {
    cwd: repoRoot,
    env: pythonEnv(repoRoot),
    encoding: "utf8",
    timeout: 120_000,
  });

  if (result.error) {
    process.stderr.write(`lifesci.toolspec: failed to run ${found.python}: ${result.error.message}\n`);
    return 1;
  }
  const stdout = (result.stdout || "").trim();
  const stderr = (result.stderr || "").trim();
  if (stdout.length > 0) process.stdout.write(`${stdout}\n`);
  if (result.status !== 0) {
    if (stderr.length > 0) process.stderr.write(`${stderr}\n`);
    process.stderr.write(
      `\nlifesci.toolspec: the Python command registry could not be exported (exit code ${result.status}).\n` +
        `Fix the failing module in python/lifesci/commands/ and retry; the tool schema is generated from it.\n`,
    );
    return result.status === null ? 1 : result.status;
  }
  if (stderr.length > 0) process.stderr.write(`${stderr}\n`);

  if (options.check) {
    process.stdout.write(`lifesci.toolspec: ${options.out} is up to date with the Python registry\n`);
    return 0;
  }
  if (!existsSync(outPath)) {
    process.stderr.write(`lifesci.toolspec: expected output ${outPath} was not written\n`);
    return 1;
  }
  const relativeOut = relative(repoRoot, outPath) || outPath;
  process.stdout.write(
    `lifesci.toolspec: wrote ${relativeOut} — ${summarizeGenerated(outPath)}\n` +
      `  interpreter: ${found.python} (Python ${found.version})\n`,
  );
  return 0;
}

process.exitCode = main();
