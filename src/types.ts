/**
 * Structural types for the DeepSeek Harness host-plugin surface.
 *
 * The plugin is deliberately zero-dependency (no `@deepseek-ai/*` imports), so
 * it describes only the small slice of the Cordis/ToolRuntime contract it
 * actually consumes. These interfaces are structural: any runtime object that
 * matches them can be passed in, which keeps the plugin installable across
 * compatible harness versions and makes the unit tests dependency-free.
 */

/** A lossless JSON value. */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

/**
 * One JSON-Schema node as emitted by `python -m lifesci.toolspec`.
 *
 * The harness enforces a JSON-Schema subset for tool *outputs*; tool
 * *parameters* are forwarded to the model provider verbatim (including
 * validation annotations the Python command registry declares, e.g. `minimum`),
 * hence the open index signature.
 */
export interface JsonSchemaValue {
  type?: string;
  properties?: Record<string, JsonSchemaValue>;
  required?: string[];
  additionalProperties?: boolean;
  items?: JsonSchemaValue;
  enum?: readonly (string | number | boolean | null)[];
  const?: string | number | boolean | null;
  oneOf?: readonly JsonSchemaValue[];
  description?: string;
  title?: string;
  default?: JsonValue;
  examples?: JsonValue;
  [keyword: string]: unknown;
}

/** One model-facing tool specification generated from the Python command registry. */
export interface ToolSpec {
  /** Dotted CLI command name, e.g. `seq.fetch`. */
  readonly command: string;
  /** Model-facing snake_case tool name, e.g. `lifesci_seq_fetch`. */
  readonly name: string;
  /** Short human-readable title, used by `presentCall`. */
  readonly title: string;
  /** Model-facing description (states what the command returns and its caveats). */
  readonly description: string;
  /** JSON Schema for the tool arguments, copied verbatim from the Python registry. */
  readonly parameters: JsonSchemaValue;
  /** Command family, e.g. `sequence` or `foundation`. */
  readonly category: string;
  /** Cooperative per-call timeout budget declared by the Python command. */
  readonly timeoutMs?: number;
  /** Example inputs shown to the model in the command documentation. */
  readonly examples?: readonly Record<string, unknown>[];
}

/** Shape of the generated `src/tool-specs.ts` payload. */
export interface GeneratedToolSpecs {
  readonly schemaVersion: number;
  readonly package: string;
  readonly version: string;
  readonly tools: readonly ToolSpec[];
}

/** One model-facing content block returned by `output.render`. */
export interface ContentBlock {
  type: "text";
  text: string;
}

/** Categories understood by the harness tool-call presenter. */
export type ToolCallKind =
  | "read"
  | "edit"
  | "delete"
  | "move"
  | "search"
  | "execute"
  | "fetch"
  | "other";

/** Generic tool-call presentation (matches the harness `GenericCallView`). */
export interface GenericCallView {
  card: "generic";
  title: string;
  kind?: ToolCallKind;
  rawInput?: unknown;
}

/** Canonical output declaration of one registered tool. */
export interface ToolOutputDefinition {
  /** Raw JSON Schema enforced against the returned value. */
  schema: JsonSchemaValue;
  /** Pure projection from validated arguments and value to model content. */
  render(args: unknown, value: unknown): ContentBlock[];
}

/** The session header facts the bridge needs to choose a working directory. */
export interface SessionHeaderLike {
  /** Absolute working directory the session was created in. */
  cwd?: string;
}

/** Minimal agent shape (`exec.agent`). */
export interface AgentLike {
  session?: {
    header?: SessionHeaderLike;
  };
}

/**
 * Runtime context handed to a tool body.
 *
 * Only the fields the bridge consumes are declared; the harness object has more.
 */
export interface ToolRunContext {
  /** Required caller-owned cancellation signal. */
  signal?: AbortSignal;
  /** Agent on whose behalf the call runs (absent for non-agent callers). */
  agent?: AgentLike;
}

/** A registered tool definition (subset of the harness `ToolDefinition`). */
export interface ToolDefinition {
  name: string;
  description: string;
  /** Raw JSON Schema for the arguments (forwarded to the model provider verbatim). */
  parameters: Record<string, unknown>;
  output: ToolOutputDefinition;
  execute(args: unknown, exec: ToolRunContext): Promise<unknown>;
  presentCall?(args: unknown): GenericCallView | undefined;
  /** Cooperative timeout budget in milliseconds, enforced by the harness. */
  timeoutMs?: number;
}

/** The scoped Cordis context handed to `apply`. */
export interface PluginContext {
  tools: {
    /** Register one tool; returns the exact disposer that unregisters it. */
    register(definition: ToolDefinition): () => void;
  };
  /**
   * Register a lifecycle effect. The callback runs immediately and returns a
   * disposer that the harness invokes when the plugin unloads.
   */
  effect(callback: () => (() => void) | void, label?: string): unknown;
}
