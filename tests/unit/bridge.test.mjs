/**
 * Unit tests for the pure half of the Python bridge (`src/bridge.ts`).
 *
 * These pin down interpreter priority, the exact child argv/environment, cache
 * and log defaults, envelope parsing, and the error message contract — all
 * without requiring a working Python environment.
 */

import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { repoRoot, loadDist } from "./_helpers.mjs";

const bridge = await loadDist("bridge.js");
const { resolveConfig } = await loadDist("config.js");

const SPEC = { command: "doctor", name: "lifesci_doctor", timeoutMs: 120_000 };

function invocationConfig(overrides = {}) {
  return resolveConfig(overrides);
}

function build(overrides = {}) {
  return bridge.buildInvocation({
    spec: SPEC,
    config: invocationConfig(),
    cwd: "/workspace/project",
    env: { PATH: "/usr/bin:/bin" },
    repoRoot: "/repo",
    platform: "linux",
    exists: () => false,
    ...overrides,
  });
}

test("repoRootFromModule resolves the package root from dist/bridge.js", () => {
  const url = pathToFileURL(join(repoRoot, "dist", "bridge.js")).href;
  assert.equal(bridge.repoRootFromModule(url), repoRoot);
  assert.equal(bridge.REPO_ROOT, repoRoot);
});

test("resolvePython: explicit config wins over env and venv", () => {
  const resolution = bridge.resolvePython({
    pythonPath: "/opt/lifesci/.venv/bin/python",
    env: { LIFESCI_PYTHON: "/env/python" },
    repoRoot: "/repo",
    platform: "linux",
    exists: () => true,
  });
  assert.deepEqual(resolution, { python: "/opt/lifesci/.venv/bin/python", source: "config" });
});

test("resolvePython: $LIFESCI_PYTHON wins over the bundled venv", () => {
  const resolution = bridge.resolvePython({
    env: { LIFESCI_PYTHON: "/env/python" },
    repoRoot: "/repo",
    platform: "linux",
    exists: () => true,
  });
  assert.deepEqual(resolution, { python: "/env/python", source: "env" });
});

test("resolvePython: bundled venv is found on posix and windows layouts", () => {
  assert.deepEqual(
    bridge.resolvePython({
      env: {},
      repoRoot: "/repo",
      platform: "linux",
      exists: (candidate) => candidate === "/repo/.venv/bin/python",
    }),
    { python: "/repo/.venv/bin/python", source: "venv" },
  );
  assert.deepEqual(
    bridge.resolvePython({
      env: {},
      repoRoot: "C:\\repo",
      platform: "win32",
      exists: (candidate) => candidate === join("C:\\repo", ".venv", "Scripts", "python.exe"),
    }),
    { python: join("C:\\repo", ".venv", "Scripts", "python.exe"), source: "venv" },
  );
});

test("resolvePython: falls back to the PATH command per platform", () => {
  assert.deepEqual(
    bridge.resolvePython({ env: {}, repoRoot: "/repo", platform: "linux", exists: () => false }),
    { python: "python3", source: "path" },
  );
  assert.deepEqual(
    bridge.resolvePython({ env: {}, repoRoot: "/repo", platform: "win32", exists: () => false }),
    { python: "python", source: "path" },
  );
});

test("resolvePython: bare commands pass through, relative paths resolve against the repo", () => {
  assert.equal(
    bridge.resolvePython({ pythonPath: "python3.11", env: {}, repoRoot: "/repo", exists: () => false }).python,
    "python3.11",
  );
  assert.equal(
    bridge.resolvePython({
      pythonPath: "./.venv/bin/python",
      env: {},
      repoRoot: "/repo",
      exists: () => false,
    }).python,
    "/repo/.venv/bin/python",
  );
});

test("buildInvocation: exact argv order, defaults and PYTHONPATH", () => {
  const invocation = build({ args: { network: false } });
  assert.equal(invocation.command, "python3");
  assert.equal(invocation.pythonSource, "path");
  assert.deepEqual(invocation.args, [
    "-m",
    "lifesci",
    "doctor",
    "--input",
    '{"network":false}',
    "--cache-dir",
    "/workspace/project/.lifesci-cache",
    "--log",
    "/workspace/project/.lifesci-cache/provenance.jsonl",
  ]);
  assert.equal(invocation.cwd, "/workspace/project");
  assert.equal(invocation.cacheDir, "/workspace/project/.lifesci-cache");
  assert.equal(invocation.logPath, "/workspace/project/.lifesci-cache/provenance.jsonl");
  assert.equal(invocation.pythonPackageDir, "/repo/python");
  assert.equal(invocation.env.PYTHONPATH, "/repo/python");
  assert.equal(invocation.env.PYTHONIOENCODING, "utf-8");
  assert.equal(invocation.env.PATH, "/usr/bin:/bin");
  assert.equal(invocation.timeoutMs, 120_000);
});

test("buildInvocation: empty args serialize to {}", () => {
  const invocation = build();
  const inputIndex = invocation.args.indexOf("--input");
  assert.equal(invocation.args[inputIndex + 1], "{}");
});

test("buildInvocation: offline and dry-run flags are appended last", () => {
  const invocation = build({ config: invocationConfig({ offline: true, dryRun: true }), args: {} });
  assert.equal(invocation.args.at(-2), "--offline");
  assert.equal(invocation.args.at(-1), "--dry-run");
});

test("buildInvocation: cacheDir/logPath config is honoured and relative paths resolve against cwd", () => {
  const invocation = build({
    config: invocationConfig({ cacheDir: "var/cache", logPath: "logs/prov.jsonl" }),
  });
  assert.equal(invocation.cacheDir, "/workspace/project/var/cache");
  assert.equal(invocation.logPath, "/workspace/project/logs/prov.jsonl");
  assert.equal(invocation.args[invocation.args.indexOf("--cache-dir") + 1], "/workspace/project/var/cache");
  assert.equal(invocation.args[invocation.args.indexOf("--log") + 1], "/workspace/project/logs/prov.jsonl");

  const absolute = build({ config: invocationConfig({ cacheDir: "/tmp/lifesci-cache" }) });
  assert.equal(absolute.cacheDir, "/tmp/lifesci-cache");
  assert.equal(absolute.logPath, "/tmp/lifesci-cache/provenance.jsonl");
});

test("buildInvocation: pythonPackageDir replaces <repo>/python and prepends PYTHONPATH", () => {
  const invocation = build({
    config: invocationConfig({ pythonPackageDir: "/custom/python" }),
    env: { PYTHONPATH: "/existing/pythonpath" },
  });
  const delimiter = process.platform === "win32" ? ";" : ":";
  assert.equal(invocation.pythonPackageDir, "/custom/python");
  assert.equal(invocation.env.PYTHONPATH, `/custom/python${delimiter}/existing/pythonpath`);
});

test("buildInvocation: extraEnv overrides the inherited environment", () => {
  const invocation = build({
    config: invocationConfig({ extraEnv: { PYTHONPATH: "/override", LIFESCI_DEBUG: "1" } }),
    env: { PYTHONPATH: "/inherited" },
  });
  assert.equal(invocation.env.PYTHONPATH, "/override");
  assert.equal(invocation.env.LIFESCI_DEBUG, "1");
});

test("buildInvocation: configured pythonPath is used and reported", () => {
  const invocation = build({ config: invocationConfig({ pythonPath: "/opt/science/python" }) });
  assert.equal(invocation.command, "/opt/science/python");
  assert.equal(invocation.pythonSource, "config");
});

test("buildInvocation: timeout precedence is spec → config default → built-in default", () => {
  assert.equal(build().timeoutMs, 120_000, "spec timeout wins");
  const noSpecTimeout = build({
    spec: { command: "doctor", name: "lifesci_doctor" },
    config: invocationConfig({ defaultTimeoutMs: 5_000 }),
  });
  assert.equal(noSpecTimeout.timeoutMs, 5_000, "config default applies when spec has none");
  const builtIn = bridge.buildInvocation({
    spec: { command: "doctor", name: "lifesci_doctor" },
    config: { ...resolveConfig(), defaultTimeoutMs: undefined },
    cwd: "/workspace/project",
    env: {},
    repoRoot: "/repo",
    platform: "linux",
    exists: () => false,
  });
  assert.equal(builtIn.timeoutMs, 300_000, "built-in default applies when config is silent");
});

test("buildInvocation: non-JSON-serializable arguments fail with BAD_INPUT", () => {
  assert.throws(
    () => build({ args: { big: 10n } }),
    (error) => {
      assert.equal(error.code, "BAD_INPUT");
      assert.match(error.message, /not JSON-serializable/);
      return true;
    },
  );
});

test("parseEnvelope: accepts a pretty-printed envelope", () => {
  const envelope = { ok: true, command: "doctor", data: { status: "ok" }, warnings: [] };
  const parsed = bridge.parseEnvelope(`${JSON.stringify(envelope, null, 2)}\n`);
  assert.deepEqual(parsed, envelope);
});

test("parseEnvelope: tolerates leading diagnostic noise before the JSON object", () => {
  const parsed = bridge.parseEnvelope('lifesci: warming up\n{"ok":true,"command":"doctor","data":{}}');
  assert.equal(parsed.ok, true);
  assert.equal(parsed.command, "doctor");
});

test("parseEnvelope: empty stdout is NO_OUTPUT and reports the stderr tail", () => {
  assert.throws(
    () => bridge.parseEnvelope("   \n", "Traceback: boom"),
    (error) => {
      assert.equal(error.code, "NO_OUTPUT");
      assert.match(error.message, /no JSON envelope/);
      assert.match(error.message, /Traceback: boom/);
      return true;
    },
  );
});

test("parseEnvelope: garbage stdout is BAD_ENVELOPE with stdout and stderr excerpts", () => {
  assert.throws(
    () => bridge.parseEnvelope("not json at all", "python: some diagnostic"),
    (error) => {
      assert.equal(error.code, "BAD_ENVELOPE");
      assert.match(error.message, /did not return a JSON envelope/);
      assert.match(error.message, /not json at all/);
      assert.match(error.message, /python: some diagnostic/);
      return true;
    },
  );
});

test("parseEnvelope: JSON that is not an object or lacks boolean ok is rejected", () => {
  assert.throws(() => bridge.parseEnvelope("[1,2,3]"), /not an envelope object/);
  assert.throws(() => bridge.parseEnvelope('{"command":"doctor"}'), /missing the boolean "ok"/);
});

test("formatEnvelopeError: stable [CODE] message plus retriable/fallbacks", () => {
  const message = bridge.formatEnvelopeError({
    ok: false,
    error: {
      code: "RATE_LIMITED",
      message: "UniProt returned 429 twice",
      retriable: true,
      fallbacks: ["FTP", "cached payload"],
    },
  });
  assert.equal(
    message,
    "[RATE_LIMITED] UniProt returned 429 twice\nretriable: true\nfallbacks: FTP, cached payload",
  );
});

test("formatEnvelopeError: missing code/message still produce a stable string", () => {
  const message = bridge.formatEnvelopeError({ ok: false, error: {} });
  assert.equal(message, "[UNKNOWN] lifesci command failed without an error message");
  assert.equal(bridge.formatEnvelopeError({ ok: false }), "[UNKNOWN] lifesci command failed without an error message");
});

test("missingPythonMessage is actionable (pythonPath / LIFESCI_PYTHON / venv)", () => {
  const message = bridge.missingPythonMessage({ command: "/usr/bin/python3", pythonPackageDir: "/repo/python" });
  assert.match(message, /python interpreter not found/);
  assert.match(message, /pythonPath/);
  assert.match(message, /LIFESCI_PYTHON/);
  assert.match(message, /\.venv\/bin\/python/);
});

test("sessionWorkingDirectory reads exec.agent.session.header.cwd", () => {
  assert.equal(bridge.sessionWorkingDirectory(), undefined);
  assert.equal(bridge.sessionWorkingDirectory({}), undefined);
  assert.equal(
    bridge.sessionWorkingDirectory({ agent: { session: { header: { cwd: "/workspace/study" } } } }),
    "/workspace/study",
  );
  assert.equal(
    bridge.sessionWorkingDirectory({ agent: { session: { header: { cwd: "   " } } } }),
    undefined,
  );
});

test("stderrTail keeps the last characters and trims", () => {
  assert.equal(bridge.stderrTail("  boom  \n"), "boom");
  const long = "x".repeat(5000);
  const tail = bridge.stderrTail(long, 100);
  assert.equal(tail.length, 101, "ellipsis plus the requested tail length");
  assert.ok(tail.startsWith("…"));
  assert.ok(tail.endsWith("x"));
});
