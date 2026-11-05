/**
 * Pure presentation helpers shared by every registered tool.
 *
 * They contain no I/O so a UI replay and a live call render identically, and
 * they are the only place the model-facing content of a result is shaped.
 */
/**
 * Human-readable label for a tool call card: the command title, falling back to
 * the dotted command name and finally the tool name for a malformed spec.
 */
export function toolTitle(spec) {
    const title = typeof spec.title === "string" ? spec.title.trim() : "";
    if (title.length > 0)
        return title;
    if (typeof spec.command === "string" && spec.command.length > 0)
        return `lifesci ${spec.command}`;
    return spec.name;
}
/**
 * Present the pending call of one lifesci tool.
 *
 * A generic card (with the command title and the raw arguments) is deliberate:
 * lifesci calls are data operations of many shapes, and the Python layer already
 * reports progress in its own envelope, so no tool-specific card is needed.
 */
export function presentCall(spec, args) {
    return {
        card: "generic",
        title: toolTitle(spec),
        kind: "other",
        rawInput: args ?? {},
    };
}
/**
 * Render a successful tool value as one pretty-printed JSON text block.
 *
 * The Python envelope's `data` payload is already deterministic JSON; printing
 * it verbatim keeps every key and number the agent may need.
 */
export function renderValue(_args, value) {
    const text = JSON.stringify(value, null, 2);
    return [{ type: "text", text: text === undefined ? "null" : text }];
}
