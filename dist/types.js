/**
 * Structural types for the DeepSeek Harness host-plugin surface.
 *
 * The plugin is deliberately zero-dependency (no `@deepseek-ai/*` imports), so
 * it describes only the small slice of the Cordis/ToolRuntime contract it
 * actually consumes. These interfaces are structural: any runtime object that
 * matches them can be passed in, which keeps the plugin installable across
 * compatible harness versions and makes the unit tests dependency-free.
 */
export {};
