/**
 * Process-level unit tests for `runProcess` / `runTool`.
 *
 * A fake interpreter is used instead of Python, so these tests exercise the
 * real spawn/kill/parse path without depending on the Python environment. On
 * Windows the fake interpreter would need a `.cmd` shim, so those two
 * end-to-end tests are skipped there; `runProcess` itself is platform-agnostic.
 */

import assert from "node:assert/strict";
import { chmodSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { loadDist, makeTempDir, processEnv, repoRoot } from "./_helpers.mjs";

const bridge = await loadDist("bridge.js");
const { resolveConfig } = await loadDist("config.js");

const isWindows = process.platform === "win32";

/** Write an executable node script that acts as a fake `python -m lifesci` CLI. */
function writeFakeInterpreter(t, body) {
  const dir = makeTempDir(t, "lifesci-fake-python-");
  const path = join(dir, isWindows ? "fake-python.cmd" : "fake-python");
  writeFileSync(path, `#!/usr/bin/env node\n${body}\n`, "utf8");
  if (!isWindows) chmodSync(path, 0o755);
  return path;
}

test("runProcess captures stdout, stderr and exit code", async () => {
  const result = await bridge.runProcess(
    process.execPath,
    ["-e", "process.stdout.write('OUT'); process.stderr.write('ERR')"],
    { cwd: repoRoot, env: processEnv(), timeoutMs: 10_000, label: "test-child" },
  );
  assert.equal(result.stdout, "OUT");
  assert.equal(result.stderr, "ERR");
  assert.equal(result.exitCode, 0);
});

test("runProcess treats a non-zero exit as a normal result", async () => {
  const result = await bridge.runProcess(process.execPath, ["-e", "process.exit(7)"], {
    cwd: repoRoot,
    env: processEnv(),
    timeoutMs: 10_000,
    label: "test-child",
  });
  assert.equal(result.exitCode, 7);
});

test("runProcess passes the environment to the child", async () => {
  const result = await bridge.runProcess(process.execPath, ["-e", "process.stdout.write(process.env.LIFESCI_TEST_VALUE ?? '')"], {
    cwd: repoRoot,
    env: { ...processEnv(), LIFESCI_TEST_VALUE: "hello-bridge" },
    timeoutMs: 10_000,
    label: "test-child",
  });
  assert.equal(result.stdout, "hello-bridge");
});

test("runProcess ENOENT uses the actionable missing-command message", async () => {
  await assert.rejects(
    bridge.runProcess("/nonexistent/python-lifesci-unit-test", [], {
      cwd: repoRoot,
      env: processEnv(),
      timeoutMs: 5_000,
      label: "lifesci doctor",
      missingCommandMessage:
        "python interpreter not found; set pythonPath config or LIFESCI_PYTHON",
    }),
    (error) => {
      assert.ok(error instanceof bridge.LifesciBridgeError);
      assert.equal(error.code, "ENOENT");
      assert.match(error.message, /python interpreter not found; set pythonPath config or LIFESCI_PYTHON/);
      return true;
    },
  );
});

test("runProcess rejects a missing working directory with BAD_CWD", async () => {
  await assert.rejects(
    bridge.runProcess(process.execPath, ["-e", ""], {
      cwd: join(repoRoot, "does", "not", "exist"),
      env: processEnv(),
      timeoutMs: 5_000,
      label: "test-child",
    }),
    (error) => {
      assert.equal(error.code, "BAD_CWD");
      assert.match(error.message, /working directory does not exist/);
      return true;
    },
  );
});

test("runProcess kills a hanging child on timeout", async () => {
  const startedAt = Date.now();
  await assert.rejects(
    bridge.runProcess(process.execPath, ["-e", "setTimeout(() => {}, 60_000)"], {
      cwd: repoRoot,
      env: processEnv(),
      timeoutMs: 300,
      label: "lifesci slow",
    }),
    (error) => {
      assert.equal(error.code, "TIMEOUT");
      assert.match(error.message, /timed out after 300 ms/);
      return true;
    },
  );
  assert.ok(Date.now() - startedAt < 10_000, "timeout must kill the child promptly");
});

test("runProcess kills a hanging child when the caller signal aborts", async () => {
  const controller = new AbortController();
  const pending = bridge.runProcess(process.execPath, ["-e", "setTimeout(() => {}, 60_000)"], {
    cwd: repoRoot,
    env: processEnv(),
    timeoutMs: 60_000,
    label: "lifesci aborted",
    signal: controller.signal,
  });
  setTimeout(() => controller.abort(), 150);
  await assert.rejects(pending, (error) => {
    assert.equal(error.code, "ABORTED");
    assert.match(error.message, /aborted/);
    return true;
  });
});

test("runProcess rejects immediately when the signal is already aborted", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    bridge.runProcess(process.execPath, ["-e", ""], {
      cwd: repoRoot,
      env: processEnv(),
      timeoutMs: 5_000,
      label: "lifesci aborted",
      signal: controller.signal,
    }),
    (error) => error.code === "ABORTED",
  );
});

test("runProcess caps buffered stdout", async () => {
  await assert.rejects(
    bridge.runProcess(process.execPath, ["-e", "process.stdout.write('x'.repeat(5000))"], {
      cwd: repoRoot,
      env: processEnv(),
      timeoutMs: 5_000,
      maxBufferBytes: 100,
      label: "lifesci chatty",
    }),
    (error) => {
      assert.equal(error.code, "OUTPUT_TOO_LARGE");
      assert.match(error.message, /stdout exceeded 100 bytes/);
      return true;
    },
  );
});

test("runTool end-to-end: spawns the configured interpreter, parses data, passes argv", { skip: isWindows }, async (t) => {
  const fakePython = writeFakeInterpreter(
    t,
    [
      'const args = process.argv.slice(2);',
      'process.stdout.write(JSON.stringify({ ok: true, command: "doctor", data: { argv: args, cwd: process.cwd() } }));',
    ].join("\n"),
  );
  const cwd = makeTempDir(t, "lifesci-run-tool-");
  const config = resolveConfig({ pythonPath: fakePython, offline: true });
  const spec = { command: "doctor", name: "lifesci_doctor", timeoutMs: 20_000 };
  const data = await bridge.runTool(spec, { network: false }, config, undefined, { cwd });

  assert.deepEqual(data.argv.slice(0, 5), ["-m", "lifesci", "doctor", "--input", '{"network":false}']);
  assert.equal(data.argv[data.argv.indexOf("--input") + 1], '{"network":false}');
  assert.equal(data.argv[data.argv.indexOf("--cache-dir") + 1], join(cwd, ".lifesci-cache"));
  assert.equal(data.argv[data.argv.indexOf("--log") + 1], join(cwd, ".lifesci-cache", "provenance.jsonl"));
  assert.equal(data.argv.at(-1), "--offline");
  assert.equal(data.cwd, cwd, "the session working directory becomes the child cwd");
});

test("runTool end-to-end: ok:false envelope becomes a [CODE] message with retriable/fallbacks", { skip: isWindows }, async (t) => {
  const fakePython = writeFakeInterpreter(
    t,
    [
      'process.stdout.write(JSON.stringify({ ok: false, command: "seq.fetch", error: {',
      '  code: "RATE_LIMITED", message: "portal refused", retriable: true, fallbacks: ["FTP"]',
      "} }));",
      "process.exit(5);",
    ].join("\n"),
  );
  const cwd = makeTempDir(t, "lifesci-run-tool-");
  const config = resolveConfig({ pythonPath: fakePython });
  await assert.rejects(
    bridge.runTool({ command: "seq.fetch", name: "lifesci_seq_fetch" }, {}, config, undefined, { cwd }),
    (error) => {
      assert.equal(
        error.message,
        "[RATE_LIMITED] portal refused\nretriable: true\nfallbacks: FTP",
      );
      return true;
    },
  );
});

test("runTool end-to-end: non-JSON stdout reports the stderr tail", { skip: isWindows }, async (t) => {
  const fakePython = writeFakeInterpreter(
    t,
    ["process.stderr.write('Traceback: the science layer crashed');", "process.stdout.write('not json');", "process.exit(7);"].join("\n"),
  );
  const cwd = makeTempDir(t, "lifesci-run-tool-");
  const config = resolveConfig({ pythonPath: fakePython });
  await assert.rejects(
    bridge.runTool({ command: "doctor", name: "lifesci_doctor" }, {}, config, undefined, { cwd }),
    (error) => {
      assert.match(error.message, /did not return a JSON envelope/);
      assert.match(error.message, /Traceback: the science layer crashed/);
      assert.match(error.message, /process exit code 7/);
      return true;
    },
  );
});

test("runTool aborts before spawn when the exec signal is already aborted", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    bridge.runTool(
      { command: "doctor", name: "lifesci_doctor" },
      {},
      resolveConfig(),
      { signal: controller.signal },
    ),
    (error) => error.code === "ABORTED",
  );
});
