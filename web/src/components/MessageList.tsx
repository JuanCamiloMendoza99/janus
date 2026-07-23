import type { Message } from "../useChat";
import { ToolBadge } from "./ToolBadge";

interface Props {
  messages: Message[];
  streaming: boolean;
  streamError: string | null;
}

export function MessageList({ messages, streaming, streamError }: Props) {
  if (messages.length === 0) {
    return (
      <div className="transcript transcript--empty">
        <p>
          Ask about a support ticket. Try a duplicate charge — with tools on, watch a{" "}
          <code>search_kb</code> badge appear before the grounded answer, and the cost of each
          model call add up in the panel.
        </p>
      </div>
    );
  }

  return (
    <div className="transcript">
      {messages.map((message, i) => {
        const last = i === messages.length - 1;
        return (
          <div key={i} className={`message message--${message.role}`}>
            <span className="message__role">{message.role}</span>
            <div className="message__body">
              {message.segments.map((segment, j) =>
                segment.kind === "text" ? (
                  <span key={j}>{segment.text}</span>
                ) : (
                  <ToolBadge key={j} name={segment.name} args={segment.args} />
                ),
              )}
              {/* A blinking cursor on the streaming assistant turn, so the answer
                  visibly arrives rather than appearing at once. */}
              {last && message.role === "assistant" && streaming && (
                <span className="cursor" aria-hidden>
                  ▌
                </span>
              )}
            </div>
          </div>
        );
      })}

      {/* stop_reason "error" is shown, never swallowed: the tool loop's iteration
          cap ends the stream this way, and a client that ignored it would render
          a request that stopped mid-thought. */}
      {streamError && (
        <div className="message message--error">
          <span className="message__role">error</span>
          <div className="message__body">{streamError}</div>
        </div>
      )}
    </div>
  );
}
