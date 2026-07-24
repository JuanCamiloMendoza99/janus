// The network layer. Everything that touches `fetch`, the response reader, and
// UTF-8 decoding lives here; the SSE framing logic it delegates to `sse.ts`,
// which stays pure and testable.

import { SSEParser, toFrame } from "./sse";
import type { ChatFrame, ChatMessage, Health, Usage } from "./types";

// In development the console is served by Vite on :5173 and the API answers on
// :8000, so calls are cross-origin and rely on the backend's CORS policy. Built
// and served by FastAPI from `web/dist`, the two are same-origin and the base is
// empty (relative). One decision, here, rather than split across a proxy config.
const API_BASE = import.meta.env.DEV ? "http://localhost:8000" : "";

export async function getHealth(): Promise<Health> {
  const response = await fetch(`${API_BASE}/health`);
  if (!response.ok) throw new Error(`/health returned ${response.status}`);
  return (await response.json()) as Health;
}

export async function getUsage(): Promise<Usage> {
  const response = await fetch(`${API_BASE}/v1/usage`);
  if (!response.ok) throw new Error(`/v1/usage returned ${response.status}`);
  return (await response.json()) as Usage;
}

/**
 * POST a conversation to `/v1/chat` and invoke `onFrame` for each SSE frame as
 * it arrives.
 *
 * The two boundary problems are handled here, not in the parser:
 *   - UTF-8 characters split across reads: `TextDecoder(..).decode(chunk,
 *     { stream: true })` holds a trailing partial byte sequence until the next
 *     read completes it, so a multi-byte character on a read boundary is never
 *     rendered as a replacement character.
 *   - A read is not a frame: raw text is handed to `SSEParser`, which only emits
 *     complete frames.
 */
export async function streamChat(
  messages: ChatMessage[],
  useTools: boolean,
  onFrame: (frame: ChatFrame) => void,
): Promise<void> {
  const response = await fetch(`${API_BASE}/v1/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, use_tools: useTools }),
  });

  if (!response.ok || !response.body) {
    // A failure before the stream opens (auth, a malformed request) comes back
    // as a normal HTTP error rather than an SSE `done`, so surface it as one.
    const detail = await safeDetail(response);
    onFrame({ event: "done", stop_reason: "error", error: detail });
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const raw of parser.push(decoder.decode(value, { stream: true }))) {
      const frame = toFrame(raw);
      if (frame) onFrame(frame);
    }
  }
}

async function safeDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail ?? `request failed with ${response.status}`;
  } catch {
    return `request failed with ${response.status}`;
  }
}
