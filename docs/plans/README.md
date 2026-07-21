# Implementation Plans

One document per phase. Each is the source of truth for what gets built in that
phase and ends with a **Verification** section — the concrete commands and
assertions that decide whether the phase is actually done, as opposed to
merely coded.

A phase is one coherent unit of work, typically 3–6 commits. On completion:
run the Verification section, tick the roadmap checkbox in the root `README.md`,
add any new ADRs to [`../architecture.md`](../architecture.md), and update the
"Current status" section of `CLAUDE.md`.

| Phase | Delivers | Status |
|---|---|---|
| 0 — Infrastructure & contracts | Repo scaffold, provider seam, ADRs, CI | ✅ done |
| [1 — Provider seam & streaming](phase-1-provider-seam.md) | Both real adapters, SSE, cost ledger | ⬜ next |
| [2 — Tool calling](phase-2-tool-calling.md) | The tool loop, two working tools | ⬜ |
| [3 — Structured outputs & caching](phase-3-structured-and-caching.md) | `/v1/triage`, proven prompt caching | ⬜ |
| [4 — Evaluation](phase-4-evals.md) | Provider comparison: cost, latency, accuracy | ⬜ |
| [5 — Prompt engineering & optimization](phase-5-prompt-optimization.md) | Versioned prompt variants, A/B on the golden set, LLM-as-judge | ⬜ |

**Phase 1 is the one that satisfies the original brief**: after it, the provider
changes with an environment variable and every request logs its cost. Phases 2–5
build on that spine.

**Phases 4 and 5 are the two axes of the same question.** Phase 4 asks which
provider and model to pay for; Phase 5 asks which prompt to ship. They share one
harness and one golden dataset.
