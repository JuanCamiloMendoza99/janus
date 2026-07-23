// The app's only stateful logic: drive one chat exchange and keep the transcript
// and the instrument readings in sync with the stream.
//
// An assistant message is modelled as an ordered list of segments — text and
// tool-call badges interleaved — rather than a single string, because a
// `tool_call` frame arrives *between* text deltas and the badge has to render
// where it happened, not bunched at the top. That ordering is the thing the
// console exists to make visible.

import { useCallback, useEffect, useState } from "react";

import { getHealth, getUsage, streamChat } from "./api";
import type { ChatMessage, Health, Usage, UsageFrame } from "./types";

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "tool"; name: string; args: Record<string, unknown> };

export interface Message {
  role: "user" | "assistant";
  segments: Segment[];
}

function textOf(message: Message): string {
  return message.segments
    .filter((s): s is { kind: "text"; text: string } => s.kind === "text")
    .map((s) => s.text)
    .join("");
}

export interface ChatState {
  messages: Message[];
  /** The usage rows for the exchange in progress or just finished — several when
   * tools ran, because each model call reports its own. */
  exchangeUsage: UsageFrame[];
  session: Usage | null;
  health: Health | null;
  useTools: boolean;
  streaming: boolean;
  /** Set when the stream ended with stop_reason "error" — shown, never swallowed. */
  streamError: string | null;
  setUseTools: (on: boolean) => void;
  send: (text: string) => void;
}

export function useChat(): ChatState {
  const [messages, setMessages] = useState<Message[]>([]);
  const [exchangeUsage, setExchangeUsage] = useState<UsageFrame[]>([]);
  const [session, setSession] = useState<Usage | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [useTools, setUseTools] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);

  const refreshPanel = useCallback(async () => {
    // The moment worth re-reading the totals is when a request finishes, not on
    // a timer. Health rarely changes between exchanges, but it is one cheap call
    // and it keeps the badge honest after a backend restart + reload.
    const [nextHealth, nextUsage] = await Promise.allSettled([getHealth(), getUsage()]);
    if (nextHealth.status === "fulfilled") setHealth(nextHealth.value);
    if (nextUsage.status === "fulfilled") setSession(nextUsage.value);
  }, []);

  useEffect(() => {
    void refreshPanel();
  }, [refreshPanel]);

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;

      const userMessage: Message = { role: "user", segments: [{ kind: "text", text: trimmed }] };
      // The payload is the committed history plus this turn — the model is sent
      // user/assistant text turns; the tool loop is internal to one request. The
      // empty assistant placeholder we append for streaming is not sent (content
      // must be non-empty), which the filter below guarantees.
      const payload: ChatMessage[] = [...messages, userMessage]
        .map((m) => ({ role: m.role, content: textOf(m) }))
        .filter((m) => m.content.length > 0);

      setMessages((prev) => [...prev, userMessage, { role: "assistant", segments: [] }]);
      setExchangeUsage([]);
      setStreamError(null);
      setStreaming(true);

      void streamChat(payload, useTools, (frame) => {
        switch (frame.event) {
          case "delta":
            setMessages((prev) => appendDelta(prev, frame.text));
            break;
          case "tool_call":
            setMessages((prev) => appendTool(prev, frame.name, frame.arguments));
            break;
          case "usage":
            setExchangeUsage((prev) => [...prev, frame]);
            break;
          case "done":
            if (frame.stop_reason === "error") {
              setStreamError(frame.error ?? "The stream ended with an error.");
            }
            break;
        }
      })
        .catch((err: unknown) => {
          setStreamError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          setStreaming(false);
          void refreshPanel();
        });
    },
    [messages, streaming, useTools, refreshPanel],
  );

  return {
    messages,
    exchangeUsage,
    session,
    health,
    useTools,
    streaming,
    streamError,
    setUseTools,
    send,
  };
}

// Immutable updates to the last (assistant) message. Deltas coalesce into the
// trailing text segment; a tool call opens a new badge segment, so subsequent
// text lands after it and the order on screen matches the order on the wire.

function appendDelta(messages: Message[], text: string): Message[] {
  return mapLast(messages, (segments) => {
    const last = segments[segments.length - 1];
    if (last && last.kind === "text") {
      return [...segments.slice(0, -1), { kind: "text", text: last.text + text }];
    }
    return [...segments, { kind: "text", text }];
  });
}

function appendTool(
  messages: Message[],
  name: string,
  args: Record<string, unknown>,
): Message[] {
  return mapLast(messages, (segments) => [...segments, { kind: "tool", name, args }]);
}

function mapLast(messages: Message[], update: (segments: Segment[]) => Segment[]): Message[] {
  if (messages.length === 0) return messages;
  const next = messages.slice();
  const last = next[next.length - 1];
  next[next.length - 1] = { ...last, segments: update(last.segments) };
  return next;
}
