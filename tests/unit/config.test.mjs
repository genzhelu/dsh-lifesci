/**
 * Unit tests for plugin configuration resolution (`src/config.ts`).
 *
 * No Python and no network are involved: `resolveConfig` is a pure validator
 * with documented defaults.
 */

import assert from "node:assert/strict";
import { homedir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { loadDist } from "./_helpers.mjs";

const { CONFIG_KEYS, DEFAULT_TIMEOUT_MS, expandHome, resolveAgainst, resolveConfig } =
  await loadDist("config.js");

test("defaults: no config yields documented defaults", () => {
  const config = resolveConfig();
  assert.equal(config.pythonPath, undefined);
  assert.equal(config.pythonPackageDir, undefined);
  assert.equal(config.cacheDir, undefined);
  assert.equal(config.logPath, undefined);
  assert.equal(config.offline, false);
  assert.equal(config.dryRun, false);
  assert.equal(config.defaultTimeoutMs, DEFAULT_TIMEOUT_MS);
  assert.equal(config.enabledTools, undefined);
  assert.deepEqual({ ...config.extraEnv }, {});
  assert.ok(Object.isFrozen(config), "resolved config must be frozen");
});

test("the bundle patch's empty config block is equivalent to no config", () => {
  assert.deepEqual({ ...resolveConfig({}) }, { ...resolveConfig() });
});

test("null config also means defaults", () => {
  assert.deepEqual({ ...resolveConfig(null) }, { ...resolveConfig() });
});

test("explicit values are validated, normalized and preserved", () => {
  const config = resolveConfig({
    pythonPath: "  /opt/lifesci/.venv/bin/python  ",
    pythonPackageDir: "python",
    cacheDir: "cache/lifesci",
    logPath: "logs/provenance.jsonl",
    offline: true,
    dryRun: true,
    defaultTimeoutMs: 1234.9,
    enabledTools: ["doctor", "lifesci_doctor", "doctor"],
    extraEnv: { LIFESCI_DEBUG: "1", HTTP_PROXY: "http://proxy:8080" },
  });
  assert.equal(config.pythonPath, "/opt/lifesci/.venv/bin/python");
  assert.equal(config.pythonPackageDir, "python");
  assert.equal(config.cacheDir, "cache/lifesci");
  assert.equal(config.logPath, "logs/provenance.jsonl");
  assert.equal(config.offline, true);
  assert.equal(config.dryRun, true);
  assert.equal(config.defaultTimeoutMs, 1234, "timeout is floored to whole milliseconds");
  assert.deepEqual([...config.enabledTools], ["doctor", "lifesci_doctor"], "duplicates removed");
  assert.deepEqual({ ...config.extraEnv }, { LIFESCI_DEBUG: "1", HTTP_PROXY: "http://proxy:8080" });
  assert.ok(Object.isFrozen(config.extraEnv));
});

test("blank string fields are treated as unset", () => {
  const config = resolveConfig({ pythonPath: "   ", cacheDir: "", logPath: "" });
  assert.equal(config.pythonPath, undefined);
  assert.equal(config.cacheDir, undefined);
  assert.equal(config.logPath, undefined);
});

test("expanding ~ uses the home directory", () => {
  assert.equal(expandHome("~"), homedir());
  assert.equal(expandHome("~/lifesci"), join(homedir(), "lifesci"));
  assert.equal(expandHome("/absolute/path"), "/absolute/path");
  assert.equal(resolveConfig({ pythonPath: "~/py/python" }).pythonPath, join(homedir(), "py", "python"));
});

test("unknown config keys are rejected with the supported key list", () => {
  assert.throws(
    () => resolveConfig({ pythonpath: "/bad/typo" }),
    (error) => {
      assert.match(error.message, /unknown lifesci config key\(s\): pythonpath/);
      for (const key of CONFIG_KEYS) assert.ok(error.message.includes(key), `message lists ${key}`);
      return true;
    },
  );
});

test("wrong field types are rejected with the offending key", () => {
  const cases = [
    [{ offline: "yes" }, /"offline" must be a boolean/],
    [{ dryRun: 1 }, /"dryRun" must be a boolean/],
    [{ defaultTimeoutMs: 0 }, /"defaultTimeoutMs" must be a positive number/],
    [{ defaultTimeoutMs: -5 }, /"defaultTimeoutMs" must be a positive number/],
    [{ defaultTimeoutMs: "fast" }, /"defaultTimeoutMs" must be a positive number/],
    [{ enabledTools: "doctor" }, /"enabledTools" must be an array/],
    [{ enabledTools: [""] }, /"enabledTools" must contain only non-empty strings/],
    [{ enabledTools: [42] }, /"enabledTools" must contain only non-empty strings/],
    [{ extraEnv: "DEBUG=1" }, /"extraEnv" must be an object/],
    [{ extraEnv: { DEBUG: 1 } }, /"extraEnv.DEBUG" must be a string/],
    [{ cacheDir: 7 }, /"cacheDir" must be a string/],
  ];
  for (const [raw, expected] of cases) assert.throws(() => resolveConfig(raw), expected);
});

test("non-object config blocks are rejected", () => {
  for (const raw of [42, "config", true, ["pythonPath"]]) {
    assert.throws(() => resolveConfig(raw), /lifesci config must be a plain object/);
  }
});

test("resolveAgainst keeps absolute paths and resolves relative ones", () => {
  assert.equal(resolveAgainst("/workspace", "/abs/cache"), "/abs/cache");
  assert.equal(resolveAgainst("/workspace", "cache"), join("/workspace", "cache"));
});
