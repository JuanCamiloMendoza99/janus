import { Composer } from "./components/Composer";
import { HealthBadge } from "./components/HealthBadge";
import { InstrumentPanel } from "./components/InstrumentPanel";
import { MessageList } from "./components/MessageList";
import { useChat } from "./useChat";

export function App() {
  const chat = useChat();

  return (
    <div className="app">
      <header className="header">
        <div className="header__brand">
          <span className="header__logo">Janus</span>
          <span className="header__tagline">instrument panel with a chat in it</span>
        </div>
        <div className="header__controls">
          <HealthBadge health={chat.health} />
          <label className="toggle" title="Send tools with the request (use_tools)">
            <input
              type="checkbox"
              checked={chat.useTools}
              onChange={(e) => chat.setUseTools(e.target.checked)}
            />
            tools
          </label>
        </div>
      </header>

      <main className="main">
        <section className="chat">
          <MessageList
            messages={chat.messages}
            streaming={chat.streaming}
            streamError={chat.streamError}
          />
          <Composer onSend={chat.send} disabled={chat.streaming} />
        </section>

        <InstrumentPanel exchangeUsage={chat.exchangeUsage} session={chat.session} />
      </main>
    </div>
  );
}
