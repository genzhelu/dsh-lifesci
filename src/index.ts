/**
 * `@genzhelu/dsh-lifesci` — DeepSeek Harness host plugin entry point.
 *
 * The plugin exposes the Python ``lifesci`` command registry as agent tools.
 * The registry is the single source of truth: ``src/tool-specs.ts`` is generated
 * from ``python -m lifesci.toolspec`` at build time, and this module simply
 * iterates it. Adding a Python command therefore needs **no** TypeScript change
 * — only a regeneration (`node scripts/generate-tool-specs.mjs`) and rebuild.
 *
 * Each tool call spawns one ``python -m lifesci <command>`` child (see
 * ``bridge.ts``); the child's JSON envelope `data` becomes the tool result, and
 * ``ok:false`` becomes a thrown Error carrying the stable ``[CODE] message``.
 */

import { runTool } from "./bridge.js";
import { resolveConfig, type ResolvedConfig } from "./config.js";
import { presentCall, renderValue } from "./presenters.js";
import { TOOL_SPECS } from "./tool-specs.js";
import type { PluginContext, ToolDefinition, ToolSpec } from "./types.js";

/** Stable Loader identity of the plugin row (`cordis.patch.yml`). */
export const name = "lifesci";

/** Services the plugin needs before `apply` runs. */
export const inject = ["tools"];

/** Output schema of every lifesci tool: the envelope `data` payload (free-form JSON object). */
const OUTPUT_SCHEMA = Object.freeze({ type: "object", additionalProperties: true });

/** Tools generated from the Python command registry, deterministically sorted by command name. */
const GENERATED_TOOLS: readonly ToolSpec[] = TOOL_SPECS.tools as unknown as readonly ToolSpec[];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Validate a generated tool spec before registration.
 *
 * The generator is trusted, but a stale or hand-edited `src/tool-specs.ts` must
 * fail at plugin load with an actionable message instead of registering a tool
 * the model cannot call correctly.
 *
 * @param tools - generated specs.
 * @throws Error describing the first malformed spec and how to regenerate.
 */
export function assertToolSpecs(tools: readonly ToolSpec[]): void {
  if (tools.length === 0) {
    throw new Error(
      "lifesci: generated src/tool-specs.ts contains no tools; " +
        "run `node scripts/generate-tool-specs.mjs` (requires the Python layer).",
    );
  }
  const seen = new Set<string>();
  for (const spec of tools) {
    const where = typeof spec?.name === "string" && spec.name.length > 0 ? spec.name : "(unnamed tool)";
    if (typeof spec?.command !== "string" || spec.command.trim().length === 0) {
      throw new Error(`lifesci: tool ${where} has no command name; regenerate src/tool-specs.ts.`);
    }
    if (typeof spec.name !== "string" || !/^[a-z0-9_]+$/.test(spec.name)) {
      throw new Error(
        `lifesci: tool ${where} has an invalid model-facing name (expected lower-case snake_case); ` +
          "regenerate src/tool-specs.ts.",
      );
    }
    if (seen.has(spec.name)) {
      throw new Error(`lifesci: duplicate tool name ${spec.name}; regenerate src/tool-specs.ts.`);
    }
    seen.add(spec.name);
    if (typeof spec.description !== "string" || spec.description.trim().length === 0) {
      throw new Error(`lifesci: tool ${spec.name} has no description; regenerate src/tool-specs.ts.`);
    }
    if (!isRecord(spec.parameters) || spec.parameters.type !== "object") {
      throw new Error(
        `lifesci: tool ${spec.name} parameters must be an object-rooted JSON Schema; ` +
          "check the Python command's input_schema and regenerate src/tool-specs.ts.",
      );
    }
    if (spec.parameters.additionalProperties !== false) {
      throw new Error(
        `lifesci: tool ${spec.name} parameters must set "additionalProperties": false ` +
          "(every field needs a description and unknown fields must be rejected).",
      );
    }
    if (!isRecord(spec.parameters.properties)) {
      throw new Error(`lifesci: tool ${spec.name} parameters have no "properties" object.`);
    }
  }
}

/**
 * Build the registry definition for one generated tool spec.
 *
 * @param spec - generated tool specification.
 * @param config - resolved plugin configuration.
 * @returns A harness-registry-ready tool definition.
 */
export function createToolDefinition(spec: ToolSpec, config: ResolvedConfig): ToolDefinition {
  const definition: ToolDefinition = {
    name: spec.name,
    // Models must be told that scientific warnings and provenance travel with the
    // payload; otherwise they routinely drop the mandatory caveats when reporting.
    description:
      spec.description +
      "\n\nResult shape: the command payload plus a `_lifesci` object containing this call's " +
      "warnings, human_review flags and compact provenance. Surface warnings and human-review flags " +
      "when reporting results; never present a result as caveat-free when `_lifesci.warnings` is non-empty.",
    // The Python registry is the source of truth for the schema; deep-clone it so
    // the harness/model-provider layer can never mutate the generated constant.
    parameters: JSON.parse(JSON.stringify(spec.parameters)) as Record<string, unknown>,
    output: {
      schema: OUTPUT_SCHEMA,
      render: (_args, value) => renderValue(_args, value),
    },
    timeoutMs: spec.timeoutMs ?? config.defaultTimeoutMs,
    async execute(args, exec) {
      return runTool(spec, args, config, exec);
    },
    presentCall: (args) => presentCall(spec, args),
  };
  return definition;
}

/**
 * Select the tools enabled by `config.enabledTools` (accepts either the
 * model-facing tool name `lifesci_seq_fetch` or the command name `seq.fetch`).
 *
 * @param tools - all generated specs.
 * @param enabled - configured filter, or undefined for "all".
 * @returns The specs to register.
 * @throws Error when the filter names tools that do not exist.
 */
export function selectTools(tools: readonly ToolSpec[], enabled: readonly string[] | undefined): ToolSpec[] {
  if (enabled === undefined) return [...tools];
  const known = new Map<string, ToolSpec>();
  for (const spec of tools) {
    known.set(spec.name, spec);
    known.set(spec.command, spec);
  }
  const unknown = enabled.filter((entry) => !known.has(entry));
  if (unknown.length > 0) {
    throw new Error(
      `lifesci config "enabledTools" names unknown tool(s): ${unknown.join(", ")}. ` +
        `Known: ${[...known.keys()].sort().join(", ")}`,
    );
  }
  const selected = new Set(enabled.map((entry) => known.get(entry) as ToolSpec));
  return tools.filter((spec) => selected.has(spec));
}

/**
 * Register every enabled lifesci tool with the harness tool runtime.
 *
 * Each registration is wrapped in a `ctx.effect` so unloading/reloading the
 * plugin unregisters exactly the tools it created.
 *
 * @param ctx - agent-scoped Cordis context (`inject: ["tools"]`).
 * @param rawConfig - raw `config:` block of the `lifesci` row (validated here).
 */
export function apply(ctx: PluginContext, rawConfig?: unknown): void {
  const config = resolveConfig(rawConfig);
  assertToolSpecs(GENERATED_TOOLS);
  const selected = selectTools(GENERATED_TOOLS, config.enabledTools);
  for (const spec of selected) {
    ctx.effect(() => {
      const dispose = ctx.tools.register(createToolDefinition(spec, config));
      return () => dispose();
    }, `lifesci:${spec.name}`);
  }
}
