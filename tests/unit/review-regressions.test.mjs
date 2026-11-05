/**
 * Regression tests for defects found by the final independent code review
 * (TypeScript host-plugin side).
 *
 *  - the model-facing tool result must carry the Python envelope's mandatory
 *    warnings / human-review flags / compact provenance (review finding #2);
 *  - a timeout or abort must terminate the whole child process tree, not only
 *    the Python parent (Foldseek/PyMOL/ssh descendants, review finding #11).
 */

import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const distUrl = new URL("../../dist/bridge.js", import.meta.url);
const bridge = await import(pathToFileURL(distUrl.pathname).href);

test("withEnvelopeDiagnostics keeps payload fields and adds warnings/human_review/provenance", () => {
  const envelope = {
    ok: true,
    command: "contacts",
    version: "0.2.0",
    offline: false,
    dry_run: false,
    cache_dir: "/tmp/cache",
    data: { n: 3, pairs: [{ a: "feature_a", b: "feature_b", min_distance: 5.0 }] },
    warnings: [{ code: "CONTACT_ABSENT_NOT_PROOF", message: "absence is not proof" }],
    human_review: [{ code: "SINGLE_WEAK_LINE_EVIDENCE", message: "review" }],
    provenance: [
      {
        kind: "http",
        url: "https://rest.uniprot.org/uniprotkb/P69905.fasta",
        status: 200,
        sha256: "abc",
        bytes: 12,
        cache: "hit",
        database: "uniprot",
        release: "2026_04",
        args: { huge: "must not be copied" },
      },
    ],
  };
  const result = bridge.withEnvelopeDiagnostics(envelope);
  assert.equal(result.n, 3, "payload fields must stay top-level");
  assert.ok(Array.isArray(result._lifesci.warnings));
  assert.equal(result._lifesci.warnings[0].code, "CONTACT_ABSENT_NOT_PROOF");
  assert.equal(result._lifesci.human_review[0].code, "SINGLE_WEAK_LINE_EVIDENCE");
  assert.equal(result._lifesci.provenance[0].url.includes("P69905"), true);
  assert.equal(result._lifesci.provenance[0].args, undefined, "provenance is compacted");
  assert.equal(result._lifesci.provenance_entries, 1);
});

test("compactDiagnostics caps provenance and marks truncation", () => {
  const provenance = Array.from({ length: 60 }, (_, index) => ({
    kind: "http",
    url: `https://example.org/${index}`,
    status: 200,
  }));
  const diagnostics = bridge.compactDiagnostics({ ok: true, data: {}, provenance });
  assert.equal(diagnostics.provenance.length, 40);
  assert.equal(diagnostics.provenance_entries, 60);
  assert.equal(diagnostics.provenance_truncated, true);
});

test(
  "timeout terminates the whole process tree, including grandchildren",
  { skip: process.platform === "win32" },
  async () => {
    const dir = mkdtempSync(join(tmpdir(), "lifesci-tree-"));
    try {
      const pidFile = join(dir, "grandchild.pid");
      // Parent spawns a long-lived grandchild (which records its pid), then idles
      // itself. Without a process-tree kill the grandchild would survive and hold
      // the inherited pipes open well past the timeout.
      const parentScript = [
        "const { spawn } = require('node:child_process');",
        "const fs = require('node:fs');",
        "const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 60000)'], { stdio: 'ignore' });",
        `fs.writeFileSync(${JSON.stringify(pidFile)}, String(child.pid));`,
        "setTimeout(() => {}, 60000);",
      ].join("\n");
      const started = Date.now();
      await assert.rejects(
        bridge.runProcess(process.execPath, ["-e", parentScript], {
          cwd: dir,
          env: process.env,
          timeoutMs: 300,
          label: "tree-kill probe",
        }),
        (error) => error.code === "TIMEOUT",
      );
      const elapsed = Date.now() - started;
      assert.ok(elapsed < 5_000, `timeout must not wait for descendants' pipes (took ${elapsed} ms)`);
      // Give the kernel a moment to reap the group, then prove the grandchild is gone.
      await new Promise((resolve) => setTimeout(resolve, 500));
      assert.ok(existsSync(pidFile), "the grandchild should have recorded its pid before the timeout");
      const grandchildPid = Number(readFileSync(pidFile, "utf8").trim());
      let alive = true;
      try {
        process.kill(grandchildPid, 0);
      } catch {
        alive = false;
      }
      assert.equal(alive, false, `grandchild pid ${grandchildPid} survived the timeout kill`);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  },
);
