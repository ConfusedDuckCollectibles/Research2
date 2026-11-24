# Research continuation

The owner has authorized continued investigation and publication in this repository. The objective is a defensible advance in efficient agent communication. Experimental progress, useful engineering, and scientific novelty must be assessed separately.

## Resume protocol

Read `research_state.json`, the latest README, frozen designs, and completed results before choosing work. Check current GitHub Actions and any available active-worker state. Respect a fresh worker lease; do not duplicate an active experiment or overwrite another worker's files. An expired lease is a recovery signal, not evidence that the previous experiment failed. Recover any completed workflow artifacts first.

Claim a new lease with an expected-head Git update before starting a new experiment. Record a unique worker ID, current UTC update time, a lease expiration no more than two hours ahead, and the exact next action. Renew during active work. At a normal stopping point, publish all reproducible work and set the status to `ready` or `blocked`, remove the active lease, and describe what remains. UTC timestamps are coordination metadata; user-facing schedules use America/Chicago.

Publish the hypothesis, primary comparison, controls, held-out split, success criteria, resource budget, and failure reporting before inspecting new held-out outcomes. Then publish the implementation before execution. Keep all seeds, failures, negative results, raw measurements and provenance. Separate tuning from evaluation. Use new evaluation content after an outcome-driven design change.

The completion-time controller follow-up is complete and independently audited; read `docs/latency_results.md`, `results/latency_audit_receipt.json`, and its frozen design before proposing further work. The fitted controller failed three of five gates. Do not rerun, replace, or retune the completed held-out experiment. A useful next step is a read-only diagnosis of decisions, charged bytes, recoveries, and timing components to identify a materially different hypothesis. Such diagnosis is exploratory and cannot isolate a causal timing decomposition from these observations. Any subsequent controller or regime requires a new preregistration and fresh held-out content/profiles, with both inline and cache/delta and the selected miss guard retained as strong comparisons. A speed benefit over an obsolete policy alone is insufficient.

Use the existing tools and bounded local or standard public CPU workflows. Preserve the existing studies. If access, runtime or usage prevents progress, record the concrete blocker instead of fabricating a result. No worker can infer the user's remaining ChatGPT quota from these files.

## Finding labels

`discoveries/` indexes measured findings, including negative findings; the name does not assert that a method is new to science. `breakthroughs/` is reserved for a candidate supported by a precise claim, strong comparisons, appropriate independent replication, a relevant prior-art review, and reproducible evidence. Publication alone does not meet that threshold.
