/**
 * Unit tests for the generated tool specs (`src/tool-specs.ts`).
 *
 * The generated file is the plugin's model-facing contract: if a spec is
 * malformed, the model receives an uncallable tool. These tests validate every
 * spec without needing Python.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { loadDist } from "./_helpers.mjs";

const { TOOL_SPECS } = await loadDist("tool-specs.js");

/** @type {readonly Record<string, any>[]} */
const specs = TOOL_SPECS.tools;

test("payload has the documented generated shape", () => {
  assert.equal(TOOL_SPECS.schemaVersion, 1);
  assert.equal(TOOL_SPECS.package, "dsh-lifesci");
  assert.match(TOOL_SPECS.version, /^\d+\.\d+\.\d+/);
  assert.ok(Array.isArray(specs), "tools must be an array");
  assert.ok(specs.length >= 1, "the registry must export at least one tool");
});

test("every tool has command, model-facing name, title, description and parameters", () => {
  for (const spec of specs) {
    assert.equal(typeof spec.command, "string", `command for ${spec.name}`);
    assert.match(spec.command, /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$/, `command ${spec.command} is lower-case dotted`);
    assert.equal(typeof spec.name, "string", `name for ${spec.command}`);
    assert.match(spec.name, /^[a-z][a-z0-9_]*$/, `tool name ${spec.name} is lower_case_snake`);
    assert.equal(
      spec.name,
      `lifesci_${spec.command.replace(/[.-]/g, "_")}`,
      `tool name must be derived from command ${spec.command}`,
    );
    assert.equal(typeof spec.title, "string");
    assert.ok(spec.title.trim().length > 0, `title for ${spec.name}`);
    assert.equal(typeof spec.description, "string");
    assert.ok(spec.description.trim().length >= 40, `description for ${spec.name} is model-facing and explanatory`);
    assert.equal(typeof spec.category, "string");
    assert.ok(spec.category.trim().length > 0, `category for ${spec.name}`);
  }
});

test("every parameter schema is an object-rooted JSON Schema that rejects unknown fields", () => {
  for (const spec of specs) {
    const parameters = spec.parameters;
    assert.equal(typeof parameters, "object", `${spec.name}.parameters must be an object`);
    assert.ok(parameters !== null && !Array.isArray(parameters), `${spec.name}.parameters must not be an array`);
    assert.equal(parameters.type, "object", `${spec.name}.parameters.type must be "object"`);
    assert.equal(
      parameters.additionalProperties,
      false,
      `${spec.name}.parameters must set additionalProperties:false`,
    );
    assert.equal(typeof parameters.properties, "object", `${spec.name}.parameters.properties must be an object`);
    assert.ok(
      parameters.properties !== null && !Array.isArray(parameters.properties),
      `${spec.name}.parameters.properties must not be an array`,
    );
    for (const [field, schema] of Object.entries(parameters.properties)) {
      assert.equal(typeof schema, "object", `${spec.name}.${field} schema must be an object`);
      assert.ok(schema !== null && !Array.isArray(schema), `${spec.name}.${field} schema must not be an array`);
      // A property may be annotation-only (e.g. a free-form `json_body` object,
      // which the harness treats as unconstrained JSON); otherwise it declares
      // exactly one type or an exact-one union.
      if (schema.type !== undefined) {
        const validType =
          typeof schema.type === "string" ||
          (Array.isArray(schema.type) &&
            schema.type.length > 0 &&
            schema.type.every((entry) => typeof entry === "string"));
        assert.ok(validType, `${spec.name}.${field}.type must be a JSON Schema type or type array`);
      }
      if (schema.type === undefined && schema.oneOf === undefined) {
        const keys = Object.keys(schema);
        assert.ok(
          keys.every((key) => ["description", "title", "default", "examples"].includes(key)),
          `${spec.name}.${field} is annotation-only, so it may only carry description/title/default/examples (got: ${keys.join(", ")})`,
        );
      }
      if (schema.oneOf !== undefined) {
        assert.ok(Array.isArray(schema.oneOf), `${spec.name}.${field}.oneOf must be an array`);
        assert.ok(schema.oneOf.length >= 2, `${spec.name}.${field}.oneOf needs at least two branches`);
      }
    }
    if (parameters.required !== undefined) {
      assert.ok(Array.isArray(parameters.required), `${spec.name}.parameters.required must be an array`);
      for (const required of parameters.required) {
        assert.ok(
          Object.hasOwn(parameters.properties, required),
          `${spec.name}.parameters.required names ${required}, which is not a declared property`,
        );
      }
    }
  }
});

test("tool names and command names are unique", () => {
  const names = specs.map((spec) => spec.name);
  const commands = specs.map((spec) => spec.command);
  assert.equal(new Set(names).size, names.length, `duplicate tool names: ${names.join(", ")}`);
  assert.equal(new Set(commands).size, commands.length, `duplicate command names: ${commands.join(", ")}`);
});

test("timeouts and examples are well-formed when present", () => {
  for (const spec of specs) {
    if (spec.timeoutMs !== undefined) {
      assert.equal(typeof spec.timeoutMs, "number", `${spec.name}.timeoutMs`);
      assert.ok(Number.isInteger(spec.timeoutMs) && spec.timeoutMs > 0, `${spec.name}.timeoutMs is a positive integer`);
    }
    if (spec.examples !== undefined) {
      assert.ok(Array.isArray(spec.examples), `${spec.name}.examples must be an array`);
      for (const example of spec.examples) {
        assert.equal(typeof example, "object", `${spec.name} example must be an object`);
        assert.ok(example !== null && !Array.isArray(example), `${spec.name} example must be an object`);
      }
    }
  }
});

test("the whole payload is lossless JSON", () => {
  const roundTripped = JSON.parse(JSON.stringify(TOOL_SPECS));
  assert.deepEqual(roundTripped, TOOL_SPECS);
});

test("specs are sorted by command name for deterministic prompts", () => {
  const commands = specs.map((spec) => spec.command);
  assert.deepEqual(commands, [...commands].sort(), "generated tools must be deterministically sorted");
});
