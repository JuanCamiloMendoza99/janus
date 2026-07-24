import type { Usage, UsageFrame } from "../types";

interface Props {
  exchangeUsage: UsageFrame[];
  session: Usage | null;
}

const usd = (n: number) => `$${n.toFixed(4)}`;
const pct = (n: number) => `${(n * 100).toFixed(0)}%`;
const int = (n: number) => n.toLocaleString("en-US");

export function InstrumentPanel({ exchangeUsage, session }: Props) {
  // Summed from the usage frames on the wire — never recomputed from a price
  // table copied into the browser. Two pricing tables is one too many.
  const exchange = exchangeUsage.reduce(
    (acc, u) => ({
      input: acc.input + u.input_tokens,
      output: acc.output + u.output_tokens,
      cacheRead: acc.cacheRead + u.cache_read_tokens,
      cacheWrite: acc.cacheWrite + u.cache_write_tokens,
      cost: acc.cost + u.cost_usd,
    }),
    { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 },
  );

  return (
    <aside className="panel">
      <section className="panel__section">
        <h2 className="panel__title">This exchange</h2>
        {exchangeUsage.length === 0 ? (
          <p className="panel__hint">No model calls yet.</p>
        ) : (
          <>
            <dl className="panel__grid">
              <Row label="Model calls" value={int(exchangeUsage.length)} />
              <Row label="Input" value={int(exchange.input)} unit="tok" />
              <Row label="Output" value={int(exchange.output)} unit="tok" />
              <Row label="Cache read" value={int(exchange.cacheRead)} unit="tok" />
              <Row label="Cache write" value={int(exchange.cacheWrite)} unit="tok" />
            </dl>
            <div className="panel__cost">{usd(exchange.cost)}</div>
            {/* Several rows when tools ran: each model call is billed on its own,
                and hiding that would make the cost figure a lie. */}
            {exchangeUsage.length > 1 && (
              <ul className="panel__calls">
                {exchangeUsage.map((u, i) => (
                  <li key={i}>
                    call {i + 1}: {usd(u.cost_usd)}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>

      <section className="panel__section">
        <h2 className="panel__title">Session</h2>
        {session ? (
          <>
            <div className="panel__cachehit" title="Fraction of prompt tokens served from cache">
              <span className="panel__cachehit-value">{pct(session.cache_hit_rate)}</span>
              <span className="panel__cachehit-label">cache hit rate</span>
            </div>
            <dl className="panel__grid">
              <Row label="Requests" value={int(session.requests)} />
              <Row label="Input" value={int(session.total_input_tokens)} unit="tok" />
              <Row label="Output" value={int(session.total_output_tokens)} unit="tok" />
              <Row label="Cache read" value={int(session.total_cache_read_tokens)} unit="tok" />
            </dl>
            <div className="panel__cost">{usd(session.total_cost_usd)}</div>
          </>
        ) : (
          <p className="panel__hint">connecting…</p>
        )}
      </section>
    </aside>
  );
}

function Row({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>
        {value}
        {unit && <span className="panel__unit"> {unit}</span>}
      </dd>
    </>
  );
}
