/**
 * Golden tests: the generated `src/tool-specs.ts` and the live Python registry
 * describe the same commands with the same model-facing schemas.
 *
 * Two guards:
 *  - `python -m lifesci.toolspec --format json` is the exact generator payload,
 *    so a stale checked-in file is caught byte-for-byte at the data level;
 *  - `python -m lifesci --list` is the CLI view, cross-checking names, tool
 *    names, titles, categories and input schemas a user sees.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { loadPluginModules, pythonSkipReason, runLifesciCli, runLifesciModule } from "./_helpers.mjs";

const { specs } = await loadPluginModules();

test("generated TOOL_SPECS equal `python -m lifesci.toolspec` output", { skip: pythonSkipReason }, (t) => {
  const result = runLifesciModule("lifesci.toolspec", ["--format", "json"]);
  if (result.error) {
    t.skip(`could not run lifesci.toolspec: ${result.error.message}`);
    return;
  }
  assert.equal(result.status, 0, `python -m lifesci.toolspec --format json failed:\n${result.stderr}`);
  const payload = JSON.parse(result.stdout);
  assert.deepEqual(
    specs,
    payload,
    "src/tool-specs.ts is stale relative to the Python command registry — run `node scripts/generate-tool-specs.mjs`",
  );
});

test("generated TOOL_SPECS match `python -m lifesci --list`", { skip: pythonSkipReason }, (t) => {
  const result = runLifesciCli(["--list"]);
  if (result.error) {
    t.skip(`could not run the Python CLI: ${result.error.message}`);
    return;
  }
  assert.equal(result.status, 0, `python -m lifesci --list failed:\n${result.stderr}`);
  const envelope = JSON.parse(result.stdout);
  assert.equal(envelope.ok, true, "--list returns a successful envelope");
  const commands = envelope.data.commands;
  assert.ok(Array.isArray(commands) && commands.length > 0, "--list returns the command registry");
  assert.deepEqual(
    commands.map((command) => command.tool_name),
    specs.tools.map((spec) => spec.name),
    "tool names and ordering match the generated specs",
  );
  for (let position = 0; position < commands.length; position += 1) {
    const command = commands[position];
    const spec = specs.tools[position];
    assert.equal(spec.command, command.name, "command names match");
    assert.equal(spec.title, command.title, `title drift for ${command.name}`);
    assert.equal(spec.category, command.category, `category drift for ${command.name}`);
    assert.deepEqual(spec.parameters, command.input_schema, `parameter schema drift for ${command.name}`);
    assert.equal(typeof command.summary, "string", `--list reports a summary for ${command.name}`);
  }
  assert.equal(specs.version, envelope.data.version, "package version matches the Python layer");
});
