import { describe, expect, it } from "vitest";

import { SSEParser, toFrame } from "./sse";

describe("SSEParser", () => {
  it("emits a complete frame", () => {
    const parser = new SSEParser();
    const frames = parser.push('event: delta\ndata: {"text":"hi"}\n\n');

    expect(frames).toEqual([{ event: "delta", data: '{"text":"hi"}' }]);
  });

  it("buffers a frame split across two reads", () => {
    // This is the bug that looks like a backend problem: the reader can hand back
    // a frame cut anywhere, including mid-field. Nothing is emitted until the
    // terminating blank line arrives.
    const parser = new SSEParser();

    expect(parser.push("event: delta\nda")).toEqual([]);
    expect(parser.push('ta: {"text":"split"}')).toEqual([]);
    expect(parser.push("\n\n")).toEqual([{ event: "delta", data: '{"text":"split"}' }]);
  });

  it("skips a keepalive ping comment", () => {
    // sse-starlette holds the connection open with `: ping` comment lines. A
    // parser that treats every line as a field would choke on them.
    const parser = new SSEParser();
    const frames = parser.push(": ping\n\nevent: done\ndata: {}\n\n");

    expect(frames).toEqual([{ event: "done", data: "{}" }]);
  });

  it("returns several frames from one chunk", () => {
    const parser = new SSEParser();
    const frames = parser.push(
      'event: delta\ndata: {"text":"a"}\n\nevent: delta\ndata: {"text":"b"}\n\n',
    );

    expect(frames.map((f) => f.data)).toEqual(['{"text":"a"}', '{"text":"b"}']);
  });

  it("joins multi-line data with newlines", () => {
    const parser = new SSEParser();
    const frames = parser.push("event: delta\ndata: line1\ndata: line2\n\n");

    expect(frames).toEqual([{ event: "delta", data: "line1\nline2" }]);
  });

  it("tolerates CRLF line endings", () => {
    const parser = new SSEParser();
    const frames = parser.push('event: delta\r\ndata: {"text":"crlf"}\r\n\r\n');

    expect(frames).toEqual([{ event: "delta", data: '{"text":"crlf"}' }]);
  });
});

describe("toFrame", () => {
  it("types a delta", () => {
    expect(toFrame({ event: "delta", data: '{"text":"hi"}' })).toEqual({
      event: "delta",
      text: "hi",
    });
  });

  it("types a tool_call with its arguments", () => {
    const frame = toFrame({
      event: "tool_call",
      data: '{"id":"t1","name":"search_kb","arguments":{"query":"double charge"}}',
    });

    expect(frame).toEqual({
      event: "tool_call",
      id: "t1",
      name: "search_kb",
      arguments: { query: "double charge" },
    });
  });

  it("surfaces a terminal done carrying an error", () => {
    // The tool loop's iteration cap ends the stream as a `done` with
    // stop_reason "error"; a client that ignored it would render a request that
    // silently stopped mid-thought.
    const frame = toFrame({
      event: "done",
      data: '{"stop_reason":"error","error":"tool loop exceeded 5 iterations"}',
    });

    expect(frame).toEqual({
      event: "done",
      stop_reason: "error",
      error: "tool loop exceeded 5 iterations",
    });
  });

  it("keeps all four token counts on a usage frame", () => {
    const frame = toFrame({
      event: "usage",
      data: '{"input_tokens":84,"output_tokens":212,"cache_read_tokens":6531,"cache_write_tokens":0,"cost_usd":0.0057}',
    });

    expect(frame).toMatchObject({
      event: "usage",
      cache_read_tokens: 6531,
      cost_usd: 0.0057,
    });
  });

  it("drops a malformed frame instead of throwing", () => {
    // A browser reading a live socket must not crash on a bad line.
    expect(toFrame({ event: "delta", data: "{not json" })).toBeNull();
  });

  it("ignores an unmodelled event type", () => {
    expect(toFrame({ event: "message", data: "{}" })).toBeNull();
  });
});
