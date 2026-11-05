/**
 * Golden tests: real data commands through the bridge, offline, against a
 * temporary cache directory. No network is used.
 */

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { loadPluginModules, pythonSkipReason, repoRoot } from "./_helpers.mjs";

const { bridge, config, specs } = await loadPluginModules();

function tempCwd(t) {
  const cwd = mkdtempSync(join(tmpdir(), "lifesci-golden-data-"));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  return cwd;
}

function execFor(cwd) {
  return { signal: new AbortController().signal, agent: { session: { header: { cwd } } } };
}

test("cache stats runs offline against a temp cache directory", { skip: pythonSkipReason }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "cache");
  if (!spec) {
    t.skip("the cache command is not in the generated tool specs");
    return;
  }
  const cwd = tempCwd(t);
  const data = await bridge.runTool(spec, { action: "stats" }, config.resolveConfig({ offline: true }), execFor(cwd));

  assert.equal(data.action, "stats");
  assert.equal(data.offline, true, "the envelope reports offline mode");
  assert.equal(data.root, join(cwd, ".lifesci-cache"));
  assert.equal(typeof data.stats, "object");
  assert.equal(data.stats.n_entries, 0, "a fresh cache starts empty");
  assert.equal(typeof data.stats.bytes, "number");
  assert.deepEqual(JSON.parse(JSON.stringify(data)), data, "payload is lossless JSON");
});

test("cache list is deterministic and does not create entries", { skip: pythonSkipReason }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "cache");
  if (!spec) {
    t.skip("the cache command is not in the generated tool specs");
    return;
  }
  const cwd = tempCwd(t);
  const offline = config.resolveConfig({ offline: true });
  const first = await bridge.runTool(spec, { action: "list" }, offline, execFor(cwd));
  const second = await bridge.runTool(spec, { action: "list" }, offline, execFor(cwd));
  assert.equal(first.action, "list");
  assert.ok(Array.isArray(first.entries));
  assert.equal(first.entries.length, 0);
  assert.deepEqual(first, second, "two offline list calls return identical data");
});

test("cache clear refuses without confirm:true (failure path through the bridge)", { skip: pythonSkipReason }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "cache");
  if (!spec) {
    t.skip("the cache command is not in the generated tool specs");
    return;
  }
  const cwd = tempCwd(t);
  await assert.rejects(
    bridge.runTool(spec, { action: "clear" }, config.resolveConfig({ offline: true }), execFor(cwd)),
    (error) => {
      assert.match(error.message, /^\[[A-Z_]+\]/, "failure carries a stable error code");
      assert.match(error.message, /confirm/i, "the message explains the missing confirmation");
      return true;
    },
  );
});

test("datasets list runs offline and reports an empty read-only registry", { skip: pythonSkipReason }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "datasets");
  if (!spec) {
    t.skip("the datasets command is not in the generated tool specs");
    return;
  }
  if (!existsSync(join(repoRoot, "python", "lifesci", "adapters", "local_dataset.py"))) {
    t.skip("python/lifesci/adapters/local_dataset.py is not available in this checkout");
    return;
  }
  const cwd = tempCwd(t);
  const data = await bridge.runTool(spec, { action: "list" }, config.resolveConfig({ offline: true }), execFor(cwd));

  assert.equal(data.action, "list");
  assert.equal(data.read_only, true, "datasets are read-only by contract");
  assert.ok(Array.isArray(data.datasets));
  assert.equal(data.n_datasets, 0, "a fresh workspace has no registered datasets");
  assert.equal(data.registry, join(cwd, ".lifesci-cache", "datasets.json"), "registry lives beside the cache");
});
