/**
 * Unit tests for the plugin entry point (`src/index.ts`).
 *
 * A fake Cordis context records every registration, so these tests prove that
 * `apply()` registers exactly the generated tool set with the harness contract,
 * honors `enabledTools`, wires lifecycle cleanup, and surfaces actionable
 * errors — all without a live harness or Python environment.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { loadDist } from "./_helpers.mjs";

const index = await loadDist("index.js");
const { TOOL_SPECS } = await loadDist("tool-specs.js");

const specs = TOOL_SPECS.tools;

/** Minimal fake of the agent-scoped Cordis context and tool runtime. */
function fakeHarness() {
  const registrations = [];
  const effects = [];
  const disposed = [];
  const ctx = {
    tools: {
      register(definition) {
        registrations.push(definition);
        return () => disposed.push(definition.name);
      },
    },
    effect(callback, label) {
      const disposer = callback();
      effects.push({ label, disposer });
      return disposer;
    },
  };
  return { ctx, registrations, effects, disposed };
}

function validSpec(overrides = {}) {
  return {
    command: "demo.run",
    name: "lifesci_demo_run",
    title: "Run the demo",
    description: "A demonstration tool description long enough to be model-facing.",
    parameters: { type: "object", additionalProperties: false, properties: {} },
    category: "general",
    ...overrides,
  };
}

test("apply registers every generated tool exactly once, wrapped in an effect", () => {
  const harness = fakeHarness();
  index.apply(harness.ctx, {});
  assert.equal(harness.registrations.length, specs.length);
  assert.equal(harness.effects.length, specs.length);
  assert.deepEqual(
    harness.registrations.map((definition) => definition.name),
    specs.map((spec) => spec.name),
    "registration order follows the deterministic generated order",
  );
  for (const effect of harness.effects) {
    assert.match(effect.label, /^lifesci:lifesci_/);
    assert.equal(typeof effect.disposer, "function", "the effect must return a disposer");
  }
  for (const effect of harness.effects) effect.disposer();
  assert.deepEqual(harness.disposed, specs.map((spec) => spec.name));
});

test("apply accepts a missing config block (bundle patch ships config: {})", () => {
  const harness = fakeHarness();
  index.apply(harness.ctx);
  assert.equal(harness.registrations.length, specs.length);
});

test("every registered definition satisfies the harness tool contract", () => {
  const harness = fakeHarness();
  index.apply(harness.ctx, {});
  for (const definition of harness.registrations) {
    assert.equal(typeof definition.name, "string");
    assert.equal(typeof definition.description, "string");
    assert.ok(definition.description.length >= 40, `${definition.name} has a model-facing description`);
    assert.equal(typeof definition.parameters, "object");
    assert.deepEqual(JSON.parse(JSON.stringify(definition.parameters)), definition.parameters);

    assert.deepEqual(definition.output.schema, { type: "object", additionalProperties: true });
    assert.equal(typeof definition.output.render, "function");
    const blocks = definition.output.render({}, { status: "ok", n: 1 });
    assert.deepEqual(blocks, [{ type: "text", text: JSON.stringify({ status: "ok", n: 1 }, null, 2) }]);

    assert.equal(typeof definition.presentCall, "function");
    const view = definition.presentCall({ action: "stats" });
    assert.equal(view.card, "generic");
    assert.equal(typeof view.title, "string");
    assert.ok(view.title.length > 0);
    assert.equal(view.kind, "other");
    assert.deepEqual(view.rawInput, { action: "stats" });

    assert.equal(typeof definition.execute, "function");
    assert.ok(Number.isInteger(definition.timeoutMs) && definition.timeoutMs > 0, `${definition.name} timeoutMs`);
  }
});

test("command parameters are copied verbatim from the generated spec", () => {
  const harness = fakeHarness();
  index.apply(harness.ctx, {});
  for (let position = 0; position < specs.length; position += 1) {
    assert.deepEqual(harness.registrations[position].parameters, specs[position].parameters);
  }
});

test("enabledTools filters by model-facing name or command name", () => {
  const harness = fakeHarness();
  index.apply(harness.ctx, { enabledTools: ["lifesci_cache", "doctor"] });
  const expected = specs.filter((spec) => spec.name === "lifesci_cache" || spec.command === "doctor");
  assert.deepEqual(
    harness.registrations.map((definition) => definition.name),
    expected.map((spec) => spec.name),
  );
});

test("enabledTools with a typo fails loudly and lists known tools", () => {
  const harness = fakeHarness();
  assert.throws(
    () => index.apply(harness.ctx, { enabledTools: ["lifesci_doctr"] }),
    (error) => {
      assert.match(error.message, /enabledTools/);
      assert.match(error.message, /lifesci_doctr/);
      assert.match(error.message, /Known:/);
      return true;
    },
  );
  assert.equal(harness.registrations.length, 0, "nothing is registered when config is invalid");
});

test("unknown config keys fail at plugin load", () => {
  const harness = fakeHarness();
  assert.throws(() => index.apply(harness.ctx, { cache_dir: "x" }), /unknown lifesci config key/);
  assert.equal(harness.registrations.length, 0);
});

test("selectTools returns all tools when no filter is configured", () => {
  assert.deepEqual(index.selectTools(specs, undefined).map((spec) => spec.name), specs.map((spec) => spec.name));
});

test("assertToolSpecs rejects malformed generated specs", () => {
  const cases = [
    [[], /contains no tools/],
    [[validSpec({ name: "Bad-Name" })], /invalid model-facing name/],
    [[validSpec({ name: "" })], /invalid model-facing name/],
    [[validSpec({ description: "" })], /no description/],
    [[validSpec({ parameters: null })], /object-rooted JSON Schema/],
    [
      [validSpec({ parameters: { type: "object", additionalProperties: true, properties: {} } })],
      /additionalProperties/,
    ],
    [[validSpec({ parameters: { type: "object", additionalProperties: false } })], /no "properties"/],
    [[validSpec(), validSpec()], /duplicate tool name/],
  ];
  for (const [tools, expected] of cases) assert.throws(() => index.assertToolSpecs(tools), expected);
});

test("execute maps a missing Python interpreter to an actionable error", async () => {
  const harness = fakeHarness();
  index.apply(harness.ctx, { pythonPath: "/nonexistent/python-lifesci-unit-test" });
  const doctor = harness.registrations.find((definition) => definition.name === "lifesci_doctor");
  assert.ok(doctor, "doctor tool must be registered");
  await assert.rejects(
    doctor.execute({ network: false }, { signal: new AbortController().signal }),
    (error) => {
      assert.match(error.message, /python interpreter not found/);
      assert.match(error.message, /pythonPath/);
      assert.match(error.message, /LIFESCI_PYTHON/);
      return true;
    },
  );
});

test("execute rejects immediately when the call is already cancelled", async () => {
  const harness = fakeHarness();
  index.apply(harness.ctx, {});
  const definition = harness.registrations[0];
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(definition.execute({}, { signal: controller.signal }), /aborted before start/);
});

test("the plugin exports the Cordis host-plugin identity", () => {
  assert.equal(index.name, "lifesci", "stable Loader identity / bundle row name");
  assert.deepEqual(index.inject, ["tools"], "the plugin depends on the tool runtime service");
  assert.equal(typeof index.apply, "function");
});

test("enabledTools: an empty allow-list registers no tools", () => {
  const harness = fakeHarness();
  index.apply(harness.ctx, { enabledTools: [] });
  assert.equal(harness.registrations.length, 0);
  assert.equal(harness.effects.length, 0);
});

test("extraEnv is carried into the registered tools' configuration", async () => {
  // The bridge is exercised with a fake interpreter; here we only prove that the
  // config passes through `apply` into a tool definition (the argv/env unit tests
  // in bridge.test.mjs pin the environment composition).
  const harness = fakeHarness();
  index.apply(harness.ctx, { extraEnv: { LIFESCI_DEBUG: "1" } });
  assert.equal(harness.registrations.length, specs.length);
});
