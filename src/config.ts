/**
 * Plugin configuration: validation and defaults.
 *
 * The host passes the `config:` block of the Cordis row (see
 * `cordis.patch.yml`) to `apply()` as a plain object. The plugin stays
 * zero-dependency, so instead of a schemastery schema it validates the block
 * itself with small, explicit checks and produces a frozen {@link ResolvedConfig}.
 *
 * Environment variables always lose to explicit config, and the bridge applies
 * its own filesystem defaults (`<repo>/.venv/bin/python`, `<cwd>/.lifesci-cache`)
 * only after config resolution, so this module is pure and trivially testable.
 */

import { homedir } from "node:os";
import { isAbsolute, resolve } from "node:path";

/** Default cooperative per-call timeout when neither the command nor config sets one. */
export const DEFAULT_TIMEOUT_MS = 300_000;

/** Every key a profile may set in the `lifesci` row's `config:` block. */
export const CONFIG_KEYS: readonly string[] = [
  "pythonPath",
  "pythonPackageDir",
  "cacheDir",
  "logPath",
  "offline",
  "dryRun",
  "defaultTimeoutMs",
  "enabledTools",
  "extraEnv",
];

/** Operator-supplied configuration (all fields optional). */
export interface LifesciPluginConfig {
  /** Python interpreter that runs `python -m lifesci`. Default: `$LIFESCI_PYTHON`, `<repo>/.venv/bin/python`, then `python3`. */
  pythonPath?: string;
  /** Directory prepended to `PYTHONPATH` (the `python/` package root). Default: `<repo>/python`. */
  pythonPackageDir?: string;
  /** Content-addressed cache directory. Default: `<session cwd>/.lifesci-cache`. */
  cacheDir?: string;
  /** Provenance JSONL log path. Default: `<cacheDir>/provenance.jsonl`. */
  logPath?: string;
  /** Never make network requests; replay cached payloads only. */
  offline?: boolean;
  /** Return planned requests instead of executing them (debugging aid). */
  dryRun?: boolean;
  /** Default per-call timeout in milliseconds when a command declares none. */
  defaultTimeoutMs?: number;
  /** Restrict registration to these tool names (`lifesci_seq_fetch`) or command names (`seq.fetch`). */
  enabledTools?: string[];
  /** Extra environment variables for the Python child process. */
  extraEnv?: Record<string, string>;
}

/** Validated configuration with defaults applied. */
export interface ResolvedConfig {
  readonly pythonPath: string | undefined;
  readonly pythonPackageDir: string | undefined;
  readonly cacheDir: string | undefined;
  readonly logPath: string | undefined;
  readonly offline: boolean;
  readonly dryRun: boolean;
  readonly defaultTimeoutMs: number;
  readonly enabledTools: readonly string[] | undefined;
  readonly extraEnv: Readonly<Record<string, string>>;
}

/** Expand a leading `~` (and `~/`) against the current home directory. */
export function expandHome(value: string): string {
  if (value === "~") return homedir();
  if (value.startsWith("~/") || value.startsWith("~\\")) {
    return resolve(homedir(), value.slice(2));
  }
  return value;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalString(raw: Record<string, unknown>, key: string): string | undefined {
  const value = raw[key];
  if (value === undefined) return undefined;
  if (typeof value !== "string") {
    throw new Error(`lifesci config "${key}" must be a string, got ${describeType(value)}`);
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? expandHome(trimmed) : undefined;
}

function optionalBoolean(raw: Record<string, unknown>, key: string): boolean {
  const value = raw[key];
  if (value === undefined) return false;
  if (typeof value !== "boolean") {
    throw new Error(`lifesci config "${key}" must be a boolean, got ${describeType(value)}`);
  }
  return value;
}

function describeType(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function positiveTimeout(raw: Record<string, unknown>): number {
  const value = raw.defaultTimeoutMs;
  if (value === undefined) return DEFAULT_TIMEOUT_MS;
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new Error(
      `lifesci config "defaultTimeoutMs" must be a positive number of milliseconds, got ${String(value)}`,
    );
  }
  return Math.floor(value);
}

function stringList(raw: Record<string, unknown>, key: string): string[] | undefined {
  const value = raw[key];
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) {
    throw new Error(`lifesci config "${key}" must be an array of strings, got ${describeType(value)}`);
  }
  const seen = new Set<string>();
  for (const entry of value) {
    if (typeof entry !== "string" || entry.trim().length === 0) {
      throw new Error(`lifesci config "${key}" must contain only non-empty strings`);
    }
    seen.add(entry.trim());
  }
  return [...seen];
}

function stringRecord(raw: Record<string, unknown>, key: string): Record<string, string> {
  const value = raw[key];
  if (value === undefined) return {};
  if (!isPlainObject(value)) {
    throw new Error(`lifesci config "${key}" must be an object of string values, got ${describeType(value)}`);
  }
  const result: Record<string, string> = {};
  for (const [name, entry] of Object.entries(value)) {
    if (name.trim().length === 0) throw new Error(`lifesci config "${key}" contains an empty variable name`);
    if (typeof entry !== "string") {
      throw new Error(`lifesci config "${key}.${name}" must be a string, got ${describeType(entry)}`);
    }
    result[name] = entry;
  }
  return result;
}

/**
 * Validate and normalize the `lifesci` row's `config:` block.
 *
 * `undefined`/`null` mean "no config" and yield the defaults (the bundle patch
 * ships `config: {}`, which is equivalent). Unknown keys are rejected so typos
 * in a profile patch surface immediately instead of being silently ignored.
 *
 * @param raw - untrusted config value from the Cordis loader.
 * @returns Frozen, fully-defaulted configuration.
 * @throws Error when the block is not an object or a field has the wrong type.
 */
export function resolveConfig(raw: unknown = undefined): ResolvedConfig {
  if (raw === undefined || raw === null) {
    return Object.freeze({
      pythonPath: undefined,
      pythonPackageDir: undefined,
      cacheDir: undefined,
      logPath: undefined,
      offline: false,
      dryRun: false,
      defaultTimeoutMs: DEFAULT_TIMEOUT_MS,
      enabledTools: undefined,
      extraEnv: Object.freeze({}),
    });
  }
  if (!isPlainObject(raw)) {
    throw new Error(`lifesci config must be a plain object, got ${describeType(raw)}`);
  }
  const unknown = Object.keys(raw).filter((key) => !CONFIG_KEYS.includes(key));
  if (unknown.length > 0) {
    throw new Error(
      `unknown lifesci config key(s): ${unknown.join(", ")} ` +
        `(supported: ${CONFIG_KEYS.join(", ")})`,
    );
  }
  const config: ResolvedConfig = Object.freeze({
    pythonPath: optionalString(raw, "pythonPath"),
    pythonPackageDir: optionalString(raw, "pythonPackageDir"),
    cacheDir: optionalString(raw, "cacheDir"),
    logPath: optionalString(raw, "logPath"),
    offline: optionalBoolean(raw, "offline"),
    dryRun: optionalBoolean(raw, "dryRun"),
    defaultTimeoutMs: positiveTimeout(raw),
    enabledTools: (() => {
      const list = stringList(raw, "enabledTools");
      return list === undefined ? undefined : Object.freeze(list);
    })(),
    extraEnv: Object.freeze(stringRecord(raw, "extraEnv")),
  });
  return config;
}

/**
 * Resolve a possibly-relative configured path against a base directory.
 * Absolute paths pass through unchanged; `~` has already been expanded.
 */
export function resolveAgainst(baseDir: string, value: string): string {
  return isAbsolute(value) ? value : resolve(baseDir, value);
}
