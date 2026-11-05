/**
 * Golden test: the checked-in `src/tool-specs.ts` is exactly what the Python
 * command registry generates (no hand edits, no stale schema).
 */

import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";

import { pythonSkipReason, repoRoot, runLifesciModule } from "./_helpers.mjs";

test("src/tool-specs.ts matches the Python registry", { skip: pythonSkipReason }, () => {
  const result = runLifesciModule("lifesci.toolspec", ["--check", join(repoRoot, "src", "tool-specs.ts")]);
  const combined = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;
  assert.equal(result.status, 0, `generated tool specs are stale:\n${combined}`);
  assert.match(combined, /up to date/);
});

test("the generated tool specs are valid TypeScript ESM (exported constant)", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(join(repoRoot, "src", "tool-specs.ts"), "utf8");
  assert.match(source, /^\/\* AUTO-GENERATED FILE/, "the file declares itself generated");
  assert.match(source, /^export const TOOL_SPECS = \{/m);
  assert.match(source, /\} as const;\s*$/);
  assert.doesNotMatch(source, /@deepseek-ai\//, "generated specs must stay dependency-free");
});
