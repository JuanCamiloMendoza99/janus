import type { Health } from "../types";

// The project's central claim rendered as one line of UI: change LLM_PROVIDER,
// restart the backend, reload — this badge changes and no frontend code did.
export function HealthBadge({ health }: { health: Health | null }) {
  if (!health) {
    return <span className="badge badge--muted">connecting…</span>;
  }
  return (
    <span className="badge" title={`environment: ${health.environment}`}>
      <span className="badge__provider">{health.provider}</span>
      <span className="badge__sep">·</span>
      <span className="badge__model">{health.model}</span>
      <span className="badge__sep">·</span>
      <span className="badge__prompt">{health.prompt}</span>
    </span>
  );
}
