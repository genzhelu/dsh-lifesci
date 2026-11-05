/**
 * Unit tests for the presentation helpers (`src/presenters.ts`).
 */

import assert from "node:assert/strict";
import test from "node:test";

import { loadDist } from "./_helpers.mjs";

const { presentCall, renderValue, toolTitle } = await loadDist("presenters.js");

const SPEC = {
  command: "seq.fetch",
  name: "lifesci_seq_fetch",
  title: "Fetch sequences from UniProt",
  description: "Fetch entries.",
  category: "sequence",
  parameters: { type: "object", additionalProperties: false, properties: {} },
};

test("presentCall returns the generic card with title/kind/rawInput", () => {
  const args = { accessions: ["P69905"] };
  assert.deepEqual(presentCall(SPEC, args), {
    card: "generic",
    title: "Fetch sequences from UniProt",
    kind: "other",
    rawInput: args,
  });
});

test("presentCall falls back to an empty rawInput for missing arguments", () => {
  assert.deepEqual(presentCall(SPEC, undefined).rawInput, {});
  assert.deepEqual(presentCall(SPEC, null).rawInput, {});
});

test("toolTitle uses title, falls back to command, then to name", () => {
  assert.equal(toolTitle(SPEC), "Fetch sequences from UniProt");
  assert.equal(toolTitle({ ...SPEC, title: "   " }), "lifesci seq.fetch");
  assert.equal(toolTitle({ ...SPEC, title: "", command: "" }), "lifesci_seq_fetch");
});

test("renderValue renders pretty-printed JSON as a single text block", () => {
  const blocks = renderValue({}, { status: "ok", nested: { count: 2 } });
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "text");
  assert.equal(blocks[0].text, JSON.stringify({ status: "ok", nested: { count: 2 } }, null, 2));
  assert.match(blocks[0].text, /"status": "ok"/);
});

test("renderValue is safe for undefined (renders null, never the string nobody wants)", () => {
  const blocks = renderValue({}, undefined);
  assert.equal(blocks[0].text, "null");
});
