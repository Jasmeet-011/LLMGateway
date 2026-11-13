"""Analysis of the load tests' raw output (M18, D-122).

Why it exists: every number in docs/RESULTS.md must come from a measured run, and a reader must
be able to regenerate it. This package turns the committed raw files under docs/results/raw/
into the figures, the accounting cross-check and measurement 8's chart; loadtest/report.py runs
it. Standard library only (PLAN.md allows no new dependency for M18), type-checked and tested
like the gateway.

Modules: `records` (k6's per-request lines), `stats` (nearest-rank percentiles, median, range),
`measures` (latency, success, overhead, per-second buckets, the cache split), `gatewaylog`
(breaker changes and per-tenant counts from the gateway's log), `accounting` (usage rows and the
cross-check), `chart` (the outage SVG).
"""
