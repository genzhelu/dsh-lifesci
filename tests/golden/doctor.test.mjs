/**
 * Golden tests: `doctor` through the real Python bridge, fully offline.
 *
 * This is the end-to-end check for the TypeScript↔Python seam: a tool
 * executes, the bridge spawns `python -m lifesci doctor --offline`, parses the
 * JSON envelope, and returns the `data` payload. No network is used.
 */

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { loadPluginModules, pythonSkipReason, repoRoot } from "./_helpers.mjs";

const { bridge, config, specs } = await loadPluginModules();

function tempCwd(t) {
  const cwd = mkdtempSync(join(tmpdir(), "lifesci-golden-doctor-"));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  return cwd;
}

function execFor(cwd) {
  return { signal: new AbortController().signal, agent: { session: { header: { cwd } } } };
}

test("doctor --offline through the bridge returns a valid offline payload", { skip: pythonSkipReason }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "doctor");
  if (!spec) {
    t.skip("the doctor command is not in the generated tool specs");
    return;
  }
  const cwd = tempCwd(t);
  const data = await bridge.runTool(spec, { network: false }, config.resolveConfig({ offline: true }), execFor(cwd));

  assert.equal(typeof data, "object");
  assert.ok(data !== null && !Array.isArray(data), "doctor data is an object");
  assert.equal(data.status, "offline", "doctor reports status 'offline' under --offline");
  assert.equal(data.lifesci_version, specs.version, "payload version matches the generated registry version");
  assert.equal(typeof data.python?.version, "string");
  assert.match(data.python.version, /^3\.(1[1-9]|[2-9]\d)/, "a supported Python 3.11+ interpreter ran the command");
  assert.ok(Array.isArray(data.checks) && data.checks.length > 0, "doctor returns machine-readable checks");
  assert.ok(data.checks.every((check) => typeof check.check === "string" && typeof check.status === "string"));
  assert.ok(existsSync(data.cache.root), "doctor creates the cache directory it reports");
  assert.equal(data.cache.root, join(cwd, ".lifesci-cache"), "cache defaults to <session cwd>/.lifesci-cache");
  assert.equal(data.cache.writable, true);
  assert.equal(data.cache.n_entries, 0);
  const networkCheck = data.checks.find((check) => check.check === "network");
  assert.ok(networkCheck, "offline runs record a skipped network check");
  assert.equal(networkCheck.detail.skipped, "offline mode");
  assert.deepEqual(JSON.parse(JSON.stringify(data)), data, "the payload is lossless JSON");
});

test("the bridge maps a command failure to a [CODE] message", { skip: pythonSkipReason }, async (t) => {
  const cwd = tempCwd(t);
  await assert.rejects(
    bridge.runTool(
      { command: "definitely.not_a_command", name: "lifesci_not_a_command" },
      {},
      config.resolveConfig({ offline: true }),
      execFor(cwd),
    ),
    (error) => {
      assert.match(error.message, /^\[USAGE\]/, "error carries the stable Python error code");
      assert.match(error.message, /unknown command/);
      return true;
    },
  );
});

test("the bridge targets the session workspace, not the plugin checkout", { skip: pythonSkipReason }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "cache");
  if (!spec) {
    t.skip("the cache command is not in the generated tool specs");
    return;
  }
  const cwd = tempCwd(t);
  const data = await bridge.runTool(spec, { action: "stats" }, config.resolveConfig({ offline: true }), execFor(cwd));
  assert.equal(data.root, join(cwd, ".lifesci-cache"));
  assert.notEqual(data.root, join(repoRoot, ".lifesci-cache"), "the session cwd wins over the plugin checkout");
});
