/**
 * Opt-in network integration tests.
 *
 * They run with `LIFESCI_NETWORK=1` (npm run test:network) and are skipped by
 * default, so the offline suite never touches the network. Keep the probes
 * small and polite: doctor's reachability checks and one tiny UniProt fetch.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { loadPluginModules, pythonSkipReason } from "../golden/_helpers.mjs";

const { bridge, config, specs } = await loadPluginModules();

const networkEnabled = process.env.LIFESCI_NETWORK === "1";
const skipReason = !networkEnabled
  ? "network tests are opt-in: set LIFESCI_NETWORK=1 (or run npm run test:network)"
  : pythonSkipReason;

function tempCwd(t) {
  const cwd = mkdtempSync(join(tmpdir(), "lifesci-network-"));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  return cwd;
}

function execFor(cwd) {
  return { signal: new AbortController().signal, agent: { session: { header: { cwd } } } };
}

test("doctor probes live portals and reports a status", { skip: skipReason, timeout: 180_000 }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "doctor");
  if (!spec) {
    t.skip("the doctor command is not in the generated tool specs");
    return;
  }
  const cwd = tempCwd(t);
  const data = await bridge.runTool(spec, { network: true }, config.resolveConfig({}), execFor(cwd));
  assert.ok(["ok", "ok-with-warnings", "degraded"].includes(data.status), `unexpected status ${data.status}`);
  assert.ok(Object.keys(data.network ?? {}).length > 0, "at least one portal probe ran");
  for (const [portal, result] of Object.entries(data.network)) {
    assert.equal(typeof result.reachable, "boolean", `${portal} probe has a reachability verdict`);
  }
});

test("seq.fetch fetches one UniProt entry and records its release", { skip: skipReason, timeout: 180_000 }, async (t) => {
  const spec = specs.tools.find((entry) => entry.command === "seq.fetch");
  if (!spec) {
    t.skip("the seq.fetch command is not in the generated tool specs");
    return;
  }
  const cwd = tempCwd(t);
  const data = await bridge.runTool(spec, { accessions: ["P69905"] }, config.resolveConfig({}), execFor(cwd));
  assert.ok(Array.isArray(data.entries), "seq.fetch returns entries");
  assert.ok(data.entries.length >= 1, "the requested accession was fetched");
  const entry = data.entries.find((candidate) => candidate.accession === "P69905") ?? data.entries[0];
  assert.equal(entry.accession, "P69905");
  assert.ok(typeof entry.sequence === "string" && entry.sequence.length > 100, "a real protein sequence came back");
  assert.ok(data.resolved || data.release || entry.release, "database release information is recorded");
});
