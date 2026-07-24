import { useState } from "react";

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
}

export function Composer({ onSend, disabled }: Props) {
  const [text, setText] = useState("");

  const submit = () => {
    if (disabled || !text.trim()) return;
    onSend(text);
    setText("");
  };

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        className="composer__input"
        value={text}
        placeholder="Ask about a ticket…  (Enter to send, Shift+Enter for a newline)"
        rows={2}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button className="composer__send" type="submit" disabled={disabled || !text.trim()}>
        {disabled ? "…" : "Send"}
      </button>
    </form>
  );
}
