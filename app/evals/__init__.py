"""The evaluation harness.

Lives under `app/` rather than in `scripts/` so it is importable and testable
without a network: the metric arithmetic is the part most likely to be quietly
wrong, and a metric that is wrong is worse than no metric — it produces a
confident recommendation from nothing. `scripts/` holds only argument parsing.
"""
