// The one genuinely hard part of the console, and therefore the only part with
// unit tests. See ADR-010: the browser's native `EventSource` only issues GET
// requests, and `POST /v1/chat` is a POST with a JSON body, so the client cannot
// use it. It reads the response body with `fetch` + a stream reader and parses
// the SSE framing by hand — this file is that parser.
//
// Three things it has to get right, each of which produces a bug that looks like
// a backend problem:
//
//   1. A read is not a frame. The reader hands back arbitrary byte boundaries, so
//      one `event:`/`data:` pair can arrive split across two reads. We buffer and
//      only emit frames terminated by a blank line.
//   2. Keepalive comments. `sse-starlette` emits `: ping` lines to hold the
//      connection open; a parser that assumes every line is a field chokes on
//      them. Lines beginning with `:` are comments and skipped.
//   3. Multi-line `data:`. The SSE spec allows several `data:` lines per event,
//      joined by newlines. We honour that even though the backend sends one.
//
// The parser is pure: strings in, `{ event, data }` records out. No DOM, no
// network — which is exactly what makes it testable. Turning a record into a
// typed `ChatFrame` is `toFrame`, kept separate so the framing and the JSON
// decoding are tested apart.

import type { ChatFrame } from "./types";

/** One parsed SSE event: its `event:` name and the joined `data:` payload. */
export interface RawFrame {
  event: string;
  data: string;
}

/**
 * Accumulates decoded text chunks and yields complete SSE frames.
 *
 * `push` returns only the frames whose terminating blank line has arrived; a
 * partial frame stays buffered until the next chunk completes it.
 */
export class SSEParser {
  private buffer = "";

  push(chunk: string): RawFrame[] {
    this.buffer += chunk;
    // Normalise CRLF to LF so the frame separator is always "\n\n" regardless of
    // whether the server frames with "\n" or "\r\n". Only complete "\r\n" pairs
    // are converted, so a lone trailing "\r" (the first half of a pair split
    // across two reads) is left for the next chunk to complete rather than being
    // turned into a spurious blank line. In SSE a "\r\n" is always a line
    // terminator, never payload, so this cannot corrupt a data value.
    this.buffer = this.buffer.replace(/\r\n/g, "\n");
    const frames: RawFrame[] = [];

    // Frames are separated by a blank line ("\n\n"). Everything up to the last
    // separator is complete; the remainder is a partial frame we keep.
    let separator = this.buffer.indexOf("\n\n");
    while (separator !== -1) {
      const block = this.buffer.slice(0, separator);
      this.buffer = this.buffer.slice(separator + 2);
      const frame = parseBlock(block);
      if (frame) frames.push(frame);
      separator = this.buffer.indexOf("\n\n");
    }
    return frames;
  }
}

/** Parse one blank-line-terminated block into a frame, or null if it carries none. */
function parseBlock(block: string): RawFrame | null {
  let event = "message"; // the SSE default when no `event:` field is present
  const dataLines: string[] = [];

  for (const rawLine of block.split("\n")) {
    // Tolerate CRLF: a stray `\r` on the end of a line would corrupt the last
    // data value or the event name otherwise.
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;

    // A line starting with ':' is a comment — this is the keepalive ping.
    if (line.startsWith(":")) continue;

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // Per spec, a single leading space after the colon is stripped.
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0) return null; // e.g. a block that was only comments
  return { event, data: dataLines.join("\n") };
}

/**
 * Turn a raw frame into a typed `ChatFrame`, or null if it is not one we model.
 *
 * The backend always sends valid JSON, but a browser reading a live socket must
 * not crash on a malformed line, so a parse failure is dropped rather than
 * thrown — the terminal `done` frame is what ends the exchange, not an
 * exception mid-stream.
 */
export function toFrame(raw: RawFrame): ChatFrame | null {
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(raw.data) as Record<string, unknown>;
  } catch {
    return null;
  }

  switch (raw.event) {
    case "delta":
      return { event: "delta", text: String(data.text ?? "") };
    case "tool_call":
      return {
        event: "tool_call",
        id: String(data.id ?? ""),
        name: String(data.name ?? ""),
        arguments: (data.arguments as Record<string, unknown>) ?? {},
      };
    case "usage":
      return {
        event: "usage",
        input_tokens: Number(data.input_tokens ?? 0),
        output_tokens: Number(data.output_tokens ?? 0),
        cache_read_tokens: Number(data.cache_read_tokens ?? 0),
        cache_write_tokens: Number(data.cache_write_tokens ?? 0),
        cost_usd: Number(data.cost_usd ?? 0),
      };
    case "done":
      return {
        event: "done",
        stop_reason: String(data.stop_reason ?? "end_turn"),
        error: data.error == null ? null : String(data.error),
      };
    default:
      return null;
  }
}
