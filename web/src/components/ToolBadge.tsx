// An inline badge for a tool call, rendered where it happened in the answer.
// The whole reason the console exists: a `search_kb` chip appearing mid-stream,
// before the grounded answer continues, is the tool loop made visible.

export function ToolBadge({ name, args }: { name: string; args: Record<string, unknown> }) {
  const summary = Object.entries(args)
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join(", ");

  return (
    <span className="tool-badge" title={JSON.stringify(args, null, 2)}>
      <span className="tool-badge__icon" aria-hidden>
        ⟳
      </span>
      <span className="tool-badge__name">{name}</span>
      {summary && <span className="tool-badge__args">{summary}</span>}
    </span>
  );
}
