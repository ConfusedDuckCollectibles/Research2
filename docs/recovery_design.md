# Frozen design: cache recovery experiment v1

This document specifies the next experiment **before its tuning or held-out outcomes are run**. The previous transport experiment and its negative results remain unchanged. Results will be written to a separate report and discussion document.

## Question and scope

Can bounded re-probing plus a recent observation window correct the previous online policy's cache-recovery trap, without adding healthy-case bytes relative to a strong cache/delta baseline?

The primary study uses the **direct-cache deployment only**. That is where the previous policy stopped probing after an early miss and could never learn that retention recovered. There is no external store, replication objective or admission-cost tradeoff in this study. All policies retain the same one-artifact cache, support the same top-level deltas, and receive the same charged post-delivery acknowledgment. Ordinary inline messages also seed that cache.

This is a synthetic transport experiment. It makes no claim about model tokens, inference speed, agent task accuracy, scientific novelty or a new general-purpose caching algorithm. Bounded probing and recent-history estimators are established techniques; the contribution being tested is correction and measurement of this specific implementation's feedback failure.

## Policies and information boundaries

The six reported policies are:

1. `inline`: always send a complete artifact.
2. `cache_delta`: choose the cheapest current inline/reference/valid-delta packet using the last acknowledged cache state. This is the strong cache baseline.
3. `inline_once_cache`: send the first artifact inline, then use `cache_delta`. It is expected to coincide with `cache_delta` in this direct deployment, but remains explicit.
4. `original_online`: the previously published cumulative-loss estimator, unchanged. Its direct-cache behavior does not depend on its external-store threshold; instantiate it with threshold 0.0.
5. `recent_reprobe`: the improved policy below, without an extra feedback channel.
6. `status_feedback`: a separately priced information intervention, described below. Its benefit must not be attributed to an improved policy with identical observations.

Ordinary policies can see the current artifact, past artifacts, their own past delivery outcomes and the last acknowledged retained revision. They cannot see current hidden eviction, phase labels, total workload length, future changes or random-number-generator state.

### Improved policy, frozen before tuning

Maintain the outcomes of the last **W** eligible cache attempts. An attempt is eligible evidence when a reference/delta was expected to hit or apply based on the preceding acknowledgment. Its observed outcome is success or recovery-required. Inline delivery is not evidence about whether the prior cache survived.

Estimate the loss probability as the fraction of failures in that window; use zero when the window is empty. Quote each current action with an intact cache and with a missing cache, using the same serialized packets and full recovery ledger as the transport simulator. Select the action with the smallest expected byte cost. Resolve equal costs by the existing deterministic action ordering.

An eligible re-probing opportunity exists only if the current zero-loss cache/delta choice is cheaper than inline and would use a reference or delta. If the risk estimate instead selects inline, increment a suppression counter. At the **Kth** such opportunity since the most recent cache attempt, perform the ordinary zero-loss cache/delta choice anyway and reset the counter. A normal cache attempt also resets it. Non-eligible handoffs do not advance the counter.

The forced attempt is a real handoff, not a free observation. Its reference/delta bytes, any recovery request and response, and final acknowledgment are all charged. No separate probe message is added or omitted from this version. Under continuing healthy retention, at most W successful re-probes remove all old failures; this does not guarantee recovery in wall-clock time or when useful references are unavailable.

Tune only the six pairs from **W in {4, 8} × K in {2, 4, 8}**. Select the pair minimizing total application payload bytes over the tuning grid, subject to no byte regression on any all-healthy tuning workload against `cache_delta`. Break ties by smaller W, then smaller K. Do not use runtime timings in selection. If no pair satisfies the healthy constraint, report that failure instead of relaxing it after inspecting outcomes.

### Separately charged current-status feedback

Before a handoff where the last acknowledged cache would make a reference/delta cheaper than inline, `status_feedback` sends a serialized cache-status request and receives a serialized response containing either the currently retained reference or null. Both packets count toward its byte total and its control-exchange count. If no such opportunity exists, it sends inline without querying.

The policy derives its cache view from the decoded response, then uses the same `cache_delta` rule. It does not receive the simulator's cache object or hidden phase as free input. The exact metadata packet format will be included in the implementation and output metadata.

This intervention assumes the status response is accurate and **no further eviction occurs between the status response and the data delivery**. The extra request/response is an additional logical round trip. The experiment counts it but does not assign a network latency. A benefit over an ordinary policy may reflect paid information, not better inference from the same observations. The feedback algorithm has no tuned parameters.

## Data generation and prefix consistency

Artifacts consist of deterministic ASCII strings distributed among 16 JSON fields, as in the previous simulator. Mutations change fixed-size selected fields. All hashes, metadata, acknowledgments and recovery payloads are serialized and counted. Seeded artifact-generation randomness and seeded loss randomness use separate generators, so extending a workload does not alter an already generated prefix.

**Loss schedules use absolute event indices, not fractions of total session length.** A short workload is a true prefix of a longer workload with the same generation settings and seed. The first event starts with an empty cache and is never treated as an eviction. A scheduled loss clears the cache immediately before a handoff, without notifying ordinary policies.

### Tuning grid: 192 workloads

Cross all of:

- String payload characters: **384, 3,072**.
- Handoffs: **12, 36**.
- Mutation modes `(update_every, changed_fields)`: **(0, 0), (3, 1), (3, 8)**.
- Cache capability: **revision_cache, delta_sync**.
- Artifact/loss seeds: **57103, 57119**; use independently derived random streams.
- Four schedules below.

| Schedule | Per-handoff hidden loss probability |
| --- | --- |
| healthy | 0 at every event |
| permanent_loss | 1 from event 1 onward |
| loss_recovery | 1 at events 1–7; 0 from event 8 onward |
| healthy_loss_recovery | 0 at events 0–3; 1 at events 4–11; 0 from event 12 onward |

Event numbering is zero-based. A workload ending before recovery remains in the dataset; the policy cannot assume more work is coming.

The best fixed comparator among `inline`, `cache_delta`, and `inline_once_cache` is chosen using tuning total bytes, separately for the two declared cache capabilities, and then frozen. The original and improved adaptive policies are also reported directly. A hindsight best-fixed choice per full workload is labeled an oracle, not an implementable predictor.

### Held-out grid: 720 workloads

Cross all of:

- String payload characters: **192, 1,536, 12,288**.
- Handoffs: **1, 3, 15, 45**.
- Mutation modes: **(0, 0), (2, 1), (5, 8)**.
- Cache capability: **revision_cache, delta_sync**.
- Seeds: **68311, 68329**, separate from tuning and all prior experiments.
- Five schedules below.

| Schedule | Per-handoff hidden loss probability |
| --- | --- |
| healthy | 0 at every event |
| permanent_loss | 1 from event 1 onward |
| loss_recovery | 1 at events 1–9; 0 from event 10 onward |
| healthy_loss_recovery | 0 at events 0–4; 1 at events 5–14; 0 from event 15 onward |
| intermittent_recovery | 0.75 at events 1–17; 0 from event 18 onward |

All grid cells receive equal inclusion. The tuning objective and aggregate-byte result are byte-weighted, so large artifacts and long workloads influence them more. Report per-capability, per-schedule, healthy-control, per-workload and worst-case results as well. Two seeds do not support broad statistical generalization.

### Independent stress grid: 16 workloads

Cross payloads **256 and 4,096**, both cache capabilities, seed **79433**, one-field mutations every four handoffs, and these four schedules:

| Schedule | Length | Hidden loss pattern |
| --- | ---: | --- |
| early_single_loss | 32 | Only event 1 loses the cache. |
| burst_cycles | 64 | Loss at events 8–15 and 32–39; healthy otherwise. |
| end_recovery | 24 | Loss at events 1–21; healthy at events 22–23. |
| alternate_three | 48 | Three-event blocks alternate healthy/loss, starting healthy. |

These cases are fixed before the new held-out results. They are not used for parameter selection or post-result adjustments.

## Accounting, correctness and metrics

Reuse the checked wire decoder and exact-reconstruction ledger. Count capability setup, all handoff bytes, normal/recovery acknowledgments, recovery requests/responses, and the status-feedback request/response when used. Give every policy the same advertised status-query capability; only policies that call it pay exchange bytes. A forced re-probe is already included in the ordinary handoff ledger and must not be counted twice.

Record application payload bytes, cache misses, forced re-probe counts, explicit status-query counts, and logical exchange counts. A logical round-trip count includes one data/ack exchange per handoff plus each recovery and each status-query exchange; this is an abstract protocol count, not a latency measurement. Optional Python decision elapsed time is recorded separately. No byte-to-token conversion is allowed.

Every delivered revision must reconstruct the exact intended JSON object from the actual wire and pass its hash check. Malformed status replies, unsupported retained references and stale delta bases must fail or invoke the specified charged recovery behavior.

Report phase-specific bytes and a recovery-use metric: for each observed recovery segment, count eligible reuse opportunities before the first successful reference/delta use. Mark segments without a success as censored and include their opportunity counts. An inline packet does not count as cache recovery evidence. Phase labels are available only to the evaluator after actions are chosen.

Primary success criteria are lower held-out bytes than `original_online` in loss/recovery schedules, exact healthy-control byte parity with the strong `cache_delta` baseline, and fewer censored recovery segments. These are separate claims, not an all-purpose dominance claim. Also report results against the tuning-selected fixed comparator, the hindsight best fixed comparator, worst-session overhead, status-feedback overhead and all failures.

## Freeze and execution order

1. Commit this design before running outcomes.
2. Implement and test packet validation, prefix consistency, hidden-state boundaries, accounting and deterministic selection.
3. Run tuning only. Save selected W/K, fixed comparator choices, design hash, implementation hashes and tuning results in a separate selection artifact.
4. Commit that selection artifact before running held-out or stress outcomes.
5. Run the frozen selected policy once on the specified held-out/stress grids. Do not alter parameters based on their outcomes.
6. Preserve negative results and label any later changed design as a new experiment with new test data.

Planned CLI: `python -m handoff_lab.recovery_experiment tune --output results/recovery_selection.json`, followed after the selection freeze by `python -m handoff_lab.recovery_experiment evaluate --selection results/recovery_selection.json --output results/recovery.json`.

The budget is six parameter candidates over 192 tuning workloads, six reported policies over 720 held-out and 16 stress workloads, plus focused tests. Use Python standard-library transport simulation only. No model/API calls, remote writes, invented discovery claims or revisions of the original result are part of this experiment.
