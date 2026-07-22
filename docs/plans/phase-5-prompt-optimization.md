# Phase 5 — Prompt engineering & optimization

**Goal.** Turn the triage prompt from a single hand-edited file into a *measured,
versioned artifact*: several playbook variants, selectable by configuration,
scored on the golden set, so the prompt Janus ships is the one the evidence chose
— and the choice is reproducible and defensible, not a matter of taste.

Where Phase 4 answers *which provider and model do we pay for* (the variable is
provider × model), Phase 5 answers *which prompt do we ship* (the variable is the
playbook variant), reusing Phase 4's harness and golden dataset. Together the two
phases cover both axes of the cost/quality surface. Anyone can write a prompt;
proving one prompt beats another on a labelled set — and knowing what that
improvement costs per ticket — is the engineering.

This phase depends on Phase 3 (the `/v1/triage` endpoint, the cached playbook)
and Phase 4 (the hand-labelled `evals/tickets.jsonl`, the runner, the metrics).
It reuses that dataset rather than inventing a second one.

## Scope

### 1. Prompt registry — the seam

Today `app/domain/prompts/playbook.md` is a single file loaded directly. Replace
that with named, versioned variants behind a registry, mirroring the provider
registry the project already has:

- Variants live under `app/domain/prompts/playbook/` (e.g. `v1-baseline.md`,
  `v2-examples.md`, `v3-terse.md`).
- A `PromptRegistry` reads a new `TRIAGE_PROMPT` setting and returns the selected
  variant's text as the `Prompt.cacheable_prefix`. The default is the current
  champion.
- Selecting a prompt is a configuration change, exactly like selecting a provider
  (ADR-006). The prompt becomes a swappable, versioned dependency — the same
  discipline the whole project applies to vendors, now applied to the prompt.

The ADR-003 constraints carry over unchanged and the registry must not weaken
them: every variant is loaded **as-is**, never templated (one variable byte
invalidates the cache prefix), and every variant must sit above the per-model
caching floor. New **ADR-009** records this decision. (ADRs are numbered in the
order they are accepted: 007 went to the tool-turn shape in Phase 2, and 008 to
structured output as a constraint in Phase 3.)

### 2. Variants worth comparing

At least three variants, each testing a *stated hypothesis*, not a cosmetic
rewrite. Candidate hypotheses:

| Variant | Hypothesis it tests |
|---|---|
| `v1-baseline` | The current playbook, as the control. |
| `v2-examples` | More worked boundary examples raise severity accuracy — at a token cost. |
| `v3-terse` | A tighter rubric holds accuracy while cutting tokens per ticket. |

Keep each variant's hypothesis in the registry metadata or this plan, **not inside
the prompt bytes** — meta-commentary that lands in the cached prefix inflates one
variant's token count and makes the cost comparison unfair. Variants must compete
on equal token footing.

### 3. A/B evaluation over the golden set

Extend Phase 4's runner to sweep the prompt-variant axis on a fixed
provider/model:

- Report per variant the same objective metrics Phase 4 defines — category and
  severity accuracy, escalation false-positive rate, PII recall, confidence
  calibration — **plus cost and latency per ticket**, because a better prompt that
  is also longer costs more, and that trade-off must be visible, not hidden.
- Honour the hold-out discipline from Phase 4: tune variants only on the train
  slice and report final numbers on the held-out slice, which is never read while
  writing prompts. A prompt hand-fitted to the tickets it was scored on measures
  nothing.

### 4. LLM-as-judge for the free-text fields

`summary` and `reasoning` are not exact-matchable; the accuracy metrics cannot
grade them. Add a judge — a separate model call that scores each field against a
fixed rubric:

- **summary** — faithful to the ticket, and genuinely triageable without opening
  the ticket.
- **reasoning** — grounded in the ticket text, no invented facts, names the signal
  that drove the classification.

Guard against the known judge failure modes, or the scores are worthless:

- A fixed rubric with a bounded integer scale; judge **one** output at a time
  against the rubric, never two variants side by side where the longer answer
  tends to win.
- Record which model and configuration judged; prefer a strong configuration
  distinct from the one under test.
- Calibrate: have the judge grade a handful of hand-scored examples first and
  report the agreement, so the reader can trust the judge before trusting its
  verdict on the variants.
- The judge spends real money — account for it in the ledger and report it.

Objective metrics on the golden set outrank the judge where they conflict; the
judge informs the free-text quality question the labels cannot answer, it does not
overrule them. This work is marked `live`.

### 5. Report and recommendation

- A committed markdown table in `docs/evals/` compares the variants across the
  objective metrics, the judge scores, and cost per ticket, so the comparison is
  versioned and its history is visible as prompts and models change.
- The README names the **champion variant and why**, stating the trade-off it
  accepts (e.g. "+3 pts severity accuracy for +18% tokens per ticket").
- The default `TRIAGE_PROMPT` is set to the champion.

## Risks

- **Overfitting the playbook to the golden set.** Hold out a slice and never look
  at it while tuning prompts (carried from Phase 4).
- **Judge bias and cost.** An LLM judge can prefer longer or more-confident answers
  regardless of correctness. Constrain it with a rubric, judge single outputs,
  calibrate against hand scores, disclose the judge model, and treat its scores as
  one input rather than the verdict.
- **A variant that drops below the caching floor** silently stops caching (ADR-003):
  the longer variant caches, a terser one might not, and an uncached variant then
  looks artificially expensive. Verify every candidate is above the floor before
  trusting its cost numbers.
- **Small dataset, wide intervals.** 40–60 tickets means a one- or two-point gap is
  likely noise. Report the sample size and resist over-reading small differences.
- **Meta-commentary skewing token cost.** Keep per-variant hypothesis notes out of
  the cached prefix so variants compete on equal token footing.

## Verification

```bash
# Sweep the prompt-variant axis on one fixed configuration (provider+model+thinking).
# Note: the runner's unit is a named *config*, not a bare --provider — sonnet is
# claude-sonnet-5 without thinking. This is the real command the plan's earlier
# `--provider anthropic` sketch stood in for (CLAUDE.md rule 3).
python scripts/run_eval.py --configs sonnet \
  --prompts v1-baseline v2-examples v3-terse --split holdout --out docs/evals/
python scripts/judge_eval.py --results docs/evals/results-prompts-holdout-*.json --calibrate
python scripts/report_prompt_eval.py

# Confirm a variant is selectable by config, end to end
TRIAGE_PROMPT=v3-terse LLM_PROVIDER=anthropic uvicorn app.main:app
curl -s localhost:8000/health   # reports the active prompt variant

# Every variant clears the token floor, counted by the vendor (free, no completion)
pytest -m live tests/test_prompts_live.py
```

Done when:

- [x] The triage prompt is loaded through the registry and selected with
      `TRIAGE_PROMPT`; `/health` reports the active variant.
- [x] At least three variants exist, each testing a stated hypothesis, each
      verified above the caching floor (`tests/test_prompts_live.py`, counted live).
- [x] The runner sweeps the prompt axis and reports the objective metrics plus
      cost and latency per variant, on a held-out slice.
- [x] An LLM-as-judge scores `summary` and `reasoning` against a fixed rubric, with
      a calibration check against hand scores (78% exact) and the judge model
      recorded (`claude-opus-4-8`).
- [x] A report in `docs/evals/prompts.md` compares the variants, and the README
      names the champion (`v2-examples`) and the trade-off it accepts.
- [x] The default `TRIAGE_PROMPT` is the champion, and the total spend of the
      prompt sweep is documented ($0.51: $0.30 triage + $0.21 judge).
- [x] ADR-009 records the prompt-registry seam.
