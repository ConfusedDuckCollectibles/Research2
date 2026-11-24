# Preregistered design: a causal completion-time controller

**Status: design only. No outcomes from this study have been run.** This follow-up preserves the source and results of the completed byte, recovery, and real-socket studies. Its implementation and tuning selection must be published before held-out timing begins.

## Question and limits

Can a small controller that learns the cost of bytes and extra replies from completed transfers outperform a validation-selected simple policy on fresh controlled TCP workloads, after counting its own computation?

The previous [socket study](network_results.md) reconstructed all 216 runs correctly, but its recent-history controller was fastest in none of the 18 cases. The simple cache policy won 11 cases and inline won seven. That result motivates changing both the decision objective and avoidable implementation overhead; it is not evidence that another adaptive policy will win.

This is an engineering experiment with actual loopback TCP, validated JSON artifacts, and deliberately imposed application pacing and receiver reply holdoff. It does not measure production networks, LLM inference, agent reasoning, token costs, or a new communication language.

Cost-aware caching and latency-aware transmission already have substantial prior art. For example, [Latency-Aware Caching with Delayed Hits](https://www.usenix.org/conference/nsdi26/presentation/keren) studies adaptive caching with latency differences; [SCALE](https://arxiv.org/abs/2611.08390) combines reuse of latent information with bandwidth/deadline-aware scheduling. This study makes no claim to invent either general idea. Its narrow contribution, if successful, would be a reproducible improvement in this protocol implementation under the declared conditions.

## Hypothesis and predeclared gates

The hypothesis is that a causal estimate of the extra time caused by recovery can improve on a byte-only decision, while a constant-size estimator and shared packet caching avoid the earlier controller's repeated hypothetical serialization.

The primary metric is **actual measured session completion time**, including packet preparation, policy computation, all wire traffic, receiver reconstruction, and recovery. Model scores are predictions of remaining action cost; they are not substituted for measurements.

The engineering target is satisfied only if all the following hold on the complete held-out grid:

1. Every planned run completes with exact receiver reconstruction and reconciled stream accounting. Failed or missing runs make this target unmet; they are never silently removed.
2. The new controller's equal-case aggregate of paired time ratios is at most **0.95** against the one globally selected simple baseline frozen from tuning.
3. That aggregate is also at most **1.00** against each simple comparator: `inline`, `cache_delta`, and the tuning-selected `miss_guard_K`. Beating a poorly generalizing validation choice alone is insufficient, and fitting a time model must not be credited with an advance when the cheap miss guard is better on the held-out aggregate.
4. For each healthy case, the median paired excess time over `cache_delta` is no greater than **max(0.5 milliseconds, 5% of that case's median cache time)**. The absolute allowance is a fixed small-host-timing tolerance, not a quantity fitted after the results.
5. The worst held-out case has a ratio of median completion times no greater than **1.25** relative to the best whole-session simple policy in that case, chosen in hindsight. This is a robustness diagnostic against an explicitly unattainable comparator, not an implementable policy or the primary baseline.

All results will be reported even if any gate fails. These are descriptive pilot targets, not statistical significance tests or criteria for a scientific breakthrough.

## Shared transport and packet preparation

Retain real IPv4 TCP on `127.0.0.1`, `TCP_NODELAY`, the exact four-byte big-endian length prefix, strict partial-read handling, and actual inline/reference/delta/GET/recovery/ACK packets. Reuse the completed study's framing and decoder where practical without changing its frozen source. No dummy payloads, extra cache-status queries, or receiver hints are allowed.

The receiver owns cache loss. It receives no source seed, expected artifact values, future artifacts, or policy state. It reconstructs artifacts only from decoded wire packets, validates hashes and delta bases, and sends a real GET on a miss. The sender validates every reply. The post-run audit compares the receiver's decoded values with the sender's intended values and reconciles bytes, frames, and stream digests in both directions.

Introduce a **common lazy packet codec**, available to every policy:

- Cache canonical artifact/reference content and valid adjacent deltas within one session, by revision/base pair. Cache reusable encoded content, not a complete envelope containing a stale event index.
- Compute a representation at most once per relevant revision/pair when a policy first requests it. Derive exact encoded lengths from these checked components. Build only the selected event envelope afterward.
- Inline requests only the content it needs. Policies that compare full and delta forms pay for both forms if those forms are needed. Do not force the inline baseline to compute an unused delta to equalize work artificially.
- Every codec access, cache miss, encoding operation, and chosen-envelope construction occurs inside the completion timer. Common code does not imply free computation.
- Fresh `Artifact` objects and empty codec caches are created for every run. No `Artifact.snapshot` cached property or fitted controller state may carry across policies or repetitions. Source values and their hashes are prepared outside the completion timer equally for all policies, as in the earlier socket study.

Focused tests must prove that assembled canonical envelopes equal the existing encoder's bytes for the same content and index, and that the real decoder reconstructs them. Include Unicode, event indices crossing a digit boundary, updates, malformed frames, missing bases, and digest failures.

This shared codec is an implementation change applied to the strong baselines as well as the proposed controller. Comparisons within this new study assess policies using that common implementation. Differences from the previous study cannot be attributed entirely to the new controller.

## Policies and common information boundary

All policies get the current source artifact, the last validated acknowledgment, and the same packet codec. Only completed traffic can update their state. The interface must not expose the case/profile object, configured rate/holdoff, source/loss seeds, schedule labels, current receiver cache, future loss flags, final session length, or future revisions.

The policy set is:

| Policy | Rule |
| --- | --- |
| `inline` | Send the full artifact on every event. |
| `cache_delta` | Send full content when no usable acknowledged base exists. Otherwise choose the smallest valid hit-path representation, using reference for the same revision or delta for a smaller adjacent update. |
| `miss_guard_K` | Use `cache_delta` while trusted. After a real GET, use inline for K−1 eligible events and attempt an ordinary cache delivery on the Kth. A successful cache attempt restores trust immediately; another GET keeps the guarded state. |
| `recent_reprobe_W4K4` | Preserve the earlier W4/K4 expected-payload-byte rule and suppression/probe behavior, implemented with the common codec. This is a reference policy, not the primary strong baseline. |
| `latency_model_lambda_K` | Use the causal time score specified below, a four-observation cache-loss window, and a charged ordinary cache attempt after K eligible suppressed opportunities. |

An eligible opportunity means the sender knows a cache/reference or adjacent-delta hit path whose encoded payload is strictly smaller than inline. It depends only on sender-observable state and current representations. Ineligible events do not advance a suppression counter. Sending inline reveals no information about whether the receiver retained its old cache.

For the recent reference policy, payload-byte costs and the existing lexical action tie-break are preserved so that it does not silently become a different controller merely because the study now accounts for framing. Actual measurement counts framing for every policy. Unit tests must compare its decisions against the existing `RecentReprobe(4, 4)` on replayed action/outcome traces. Any unavoidable departure requires an explicit pre-outcome design amendment.

The miss guard is deliberately cheap and may outperform the fitted model. Its inclusion tests whether timing estimation adds value beyond reacting to observed misses and periodically checking recovery.

## The causal time model

There are at most two useful choices at each event: inline and one cache-dependent form. A cache-dependent form is a reference when the last ACK identifies the current revision, or a valid adjacent delta when its base was acknowledged. Forms larger than inline are ineligible. The controller does not enumerate external-store routes or serialize hypothetical complete recovery paths repeatedly.

Let all lengths below include their actual four-byte frame prefixes:

- `I`: the inline data frame length.
- `C`: the eligible reference/delta data frame length.
- `A`: the ACK frame length for the current reference.
- `G`: the GET frame length for an absent base (`have: null`).
- `S`: the full snapshot recovery frame length for the current artifact.

Cache failures in this experiment clear the receiver cache completely. A failed cache attempt therefore needs the real GET plus full snapshot followed by an ACK. There is no assumption of free stale-base recovery. The codec can quote these deterministic protocol lengths from sender-owned content; quoting them is charged computation and conveys no information about whether a loss actually happened.

Let `p` be the mean of the last four observed cache-attempt failures, initialized to zero. A failure is inferred from a **validated receiver-originated GET**, not from a simulator loss flag. Successful expected-hit reference/delta attempts append zero; attempts requiring a GET append one. Inline does not update this window.

Fit nonnegative coefficients `a` (seconds per receiver reply) and `b` (seconds per KiB of total framed traffic). Predict:

```text
inline_score = a + b * (I + A) / 1024
cache_hit_score = a + b * (C + A) / 1024
cache_expected_score = cache_hit_score + p * (a + b * (G + S) / 1024)
```

The equivalent cache-versus-inline comparison is:

```text
predicted_byte_saving = b * (I - C) / 1024
predicted_miss_penalty = p * (a + b * (G + S) / 1024)
```

Choose cache when saving is at least penalty; otherwise choose inline. A score tie favors the smaller hit-path representation. When `p == 0`, take the same eligible cache path as the strong cache baseline without solving the regression or comparing hypothetical missing-cache paths. Initial events with no eligible cache form send inline.

`a` is an effective observed cost of another receiver reply/exchange, including relevant local work. It is **not an estimate presented as a real WAN RTT**. `b` similarly mixes imposed pacing and byte-related processing. The model is an approximation: reference/delta/full decoding costs need not obey the same linear relationship.

### Measurements and constant-size fit

For each completed handoff, record only observations available to the sender:

- actual total framed bytes sent plus received for this handoff;
- number of validated receiver replies (one ACK, or GET then ACK);
- whether a validated GET occurred;
- measured action-service duration, starting immediately before constructing the selected event envelope and ending after validation of its final ACK.

This duration includes selected-envelope construction, socket transfer, any recovery encoding, receiver work, and all imposed waits. Already incurred packet-quote preparation and policy selection are sunk costs outside this fitted response; they remain inside the **primary completion measurement**. Fit/observation work is also charged to completion time. The score therefore estimates the remaining action, not a free or exact end-to-end prediction.

Use features `x = (reply_count, framed_bytes / 1024)` and response `t` in seconds. Keep only `M = sum(weight * x*x^T)`, `z = sum(weight * x*t)`, and `u = sum(weight * t*t)`. After each completed observation:

```text
M = lambda * M + outer(x, x)
z = lambda * z + x * t
u = lambda * u + t * t
```

Initialize all sufficient statistics to zero. There is **no ridge prior, fitted intercept, true-rate prior, or separate calibration traffic**. Solve the two-parameter nonnegative least-squares problem analytically when a decision needs it:

1. If there are no observations, use `a = b = 0` and the hit-path tie-break.
2. If `det(M) <= 1e-12 * M[0,0] * M[1,1]`, use `a = max(0, z[0] / M[0,0])`, `b = 0`. This explicit underidentified fallback attributes observed service time to replies; it does not invent a bandwidth estimate.
3. Otherwise consider the feasible unconstrained two-by-two solution, the best `a`-only solution, the best `b`-only solution, and zero. Minimize `v^T*M*v - 2*z^T*v`. Values within `1e-12 * max(u, 1e-12)` count as tied; prefer `a`-only, then `b`-only, then the interior solution, then zero. Reject nonfinite measurements/parameters as errors rather than letting them affect a silent fallback.

Updating and fitting require constant-size arithmetic, not an optimizer, growing history, or full candidate simulations. Both kinds of work are separately timed and included in completion.

### Charged re-probing

If the model chooses inline despite an eligible cache option, increment its suppression counter. On the Kth such opportunity, send the ordinary cache-dependent packet instead. Reset the counter whenever a cache-dependent packet is sent. A forced probe can fail and then pays the complete GET, recovery response, extra holdoff, and ACK. It is not a status request and it does not obtain free feedback.

```text
choose(current, last_ack, codec):
    obtain the needed current packet metadata
    if no smaller eligible cache form: return INLINE
    if p == 0: reset suppression; return CACHE_FORM
    fit a, b from completed observations only
    if cache_expected_score <= inline_score:
        reset suppression; return CACHE_FORM
    increment eligible suppression
    if suppression == K: reset suppression; return CACHE_FORM_AS_PROBE
    return INLINE

observe(validated completed transfer):
    update the two-feature sufficient statistics
    if a cache form was attempted:
        append whether a validated GET was observed to the W4 window
```

## Fresh source, phase, and profile grids

Source artifacts have 16 fields and the same deterministic character generator as earlier studies, but fresh source seeds. Change one field at every fifth event, beginning with event 5. Payload characters are not the same as JSON or framed stream bytes. All policies/repetitions of a case see identical source values and receiver loss events.

### Tuning grid

| Source payload characters | Source seed |
| ---: | ---: |
| 1,024 | 116101 |
| 8,192 | 116129 |

| Workload | Handoffs | Receiver loss before absolute event indices |
| --- | ---: | --- |
| healthy | 20 | None |
| early_recovery | 14 | 1–5 inclusive; retain from 6 onward |
| late_recovery | 20 | 7–12 inclusive; retain from 13 onward |

| Profile | Receiver holdoff per reply | Framed bytes/second per direction |
| --- | ---: | ---: |
| unshaped | 0 | Unlimited |
| holdoff12_rate750k | 12 ms | 750,000 |

Evaluate nine tuning policies: inline, cache_delta, miss_guard K=2, miss_guard K=4, recent W4/K4, and the four model combinations **lambda in {0.5, 0.9} × K in {2, 4}**. W remains four. There are **2 × 3 × 2 × 9 × 3 = 324 measured runs**.

### Held-out grid

| Source payload characters | Source seed |
| ---: | ---: |
| 1,536 | 127003 |
| 12,288 | 127031 |

| Workload | Handoffs | Receiver loss before absolute event indices |
| --- | ---: | --- |
| healthy | 24 | None |
| short_single | 7 | Event 2 only |
| burst_recovery | 24 | 2–9 inclusive; retain from 10 onward |
| intermittent_recovery | 18 | Probability 0.6 at events 1–10 inclusive; retain from 11 onward |

For intermittent loss, the receiver creates a separate `random.Random(source_seed ^ 0x1A2B3C4D)` generator, draws once for every absolute event including event zero, and applies a loss only within the declared phase when the draw is below 0.6. This source-independent loss stream is replayed identically for all policies and repeats. Its seed is receiver configuration, never a policy input.

| Profile | Receiver holdoff per reply | Framed bytes/second per direction |
| --- | ---: | ---: |
| unshaped | 0 | Unlimited |
| holdoff4_rate400k | 4 ms | 400,000 |
| holdoff16_rate1200k | 16 ms | 1,200,000 |

The held-out policies are inline, cache_delta, the tuning-selected miss guard, recent W4/K4, and the tuning-selected model. There are **2 × 4 × 3 × 5 × 3 = 360 measured runs**, comprising 24 cases and three timing repetitions. These are 24 controlled cases, not 360 independent workload samples. Source seeds are paired with sizes, so content-seed effects cannot be separated from size effects.

All loss rules and update rules depend on absolute event indices, not the final length, policy actions, elapsed time, or model parameters. Prefix tests must prove that shorter traces are true prefixes. Replay the same completed observations to test controller prefix invariance; a time-learning controller need not make identical choices across independent real-timing repetitions. Any resulting variation in byte counts/actions is reported, not treated as a determinism violation or hidden by averaging.

## Real timing and pacing

Use the previous framing implementation's pre-send pacing: chunks of at most 1,024 bytes, no accumulated idle credit, and all waits before transmitted bytes, including the last ACK. Holdoff is applied before every receiver GET and ACK exactly once. These are imposed service/link behaviors, not measured WAN delays. The profile may be used by the transport layer but must not be passed to a policy or its estimator.

The completion timer starts immediately before the capability frame after TCP setup and policy initialization, and ends after final ACK validation and the final controller observation/bookkeeping. It includes every handoff's codec preparation, selection, model update, real transfer, reconstruction, recovery, and imposed wait. Source construction, connection setup, and teardown are separately recorded. They cannot be moved into or out of the boundary for only one policy.

Record whole completion time, packet-codec preparation/construction time, policy selection time, estimator/observation time, action-service time, actual stream bytes/frames/digests at both endpoints, all GET/ACK counts, chosen actions, model parameters/predictions, suppression/probe events, and receiver output digests. The event trace must support deterministic replay of policy decisions from **past recorded observations**, without accessing the receiver's hidden loss trace. Record environment metadata, Python/platform/CPU count, clock properties, source/design hashes, and execution order.

No background CPU-heavy benchmark should intentionally run concurrently. Host noise that still occurs is part of the observation and does not justify deleting a cell or rerunning only an unfavorable policy.

## Selection and analysis frozen before outcomes

For case `c`, policy `p`, baseline `q`, and repetition `r`, let `T[c,p,r]` be actual successful completion time. Define:

```text
case_ratio(p, q, c) = median over the three paired repetitions of T[c,p,r] / T[c,q,r]
aggregate_ratio(p, q) = exp(mean over cases of log(case_ratio(p, q, c)))
```

Cases have equal weight; this is not a pooled byte-weighted or total-time speedup. Provide all three times and all three paired ratios for every comparison. The aggregate describes this deliberately chosen mixture, not an estimate of a production workload distribution.

Tuning selections use `aggregate_ratio(candidate, cache_delta)` on the 12 tuning cases:

1. Select K for the miss guard by the lower score; exact ties favor K=2.
2. Select the new model's lambda/K pair by the lower score; exact ties favor smaller K, then larger lambda.
3. Select **one global simple baseline** from inline, cache_delta, and the selected miss guard by the lower score. Exact ties favor cache_delta, then inline, then miss guard.

No selector can choose a baseline by hidden schedule or configured profile. A candidate with any failed/missing tuning cell is ineligible; all such failures remain in the report. If a complete valid selection cannot be made, stop before held-out execution and report that limitation. Publish selection, all tuning results, source hashes, and this design hash before held-out timing. Do not change parameters or implementation based on held-out observations.

On held-out results, report the fixed selected baseline and comparisons with both inline and cache_delta, the selected miss guard, and recent W4/K4. Report healthy, short, recovery, and intermittent cases separately, plus the aggregate, worst-case ratios, packet/action counts, prediction errors, and computation overhead.

The separate **per-case hindsight reference** chooses the fastest median whole-session policy among inline, cache_delta, and the selected miss guard using that held-out case's results. Its ratio uses ratios of medians and is explicitly labeled as such. It is not the tuning-selected comparator, not an online policy, and not an oracle that switches actions at each event. Do not mix it with the primary median-of-paired-ratios metric.

Report prediction error using only predictions made before each observed transfer. Forced probes must be marked so an inline choice's counterfactual score is not compared to the probe's observed cost. Unobserved counterfactual action times are unknown; do not fill them with a model score and call them measurements.

## Budget, failures, and feasibility

Use repetition as the outer block, shuffle cases within it, then shuffle the candidate policies within each case. Use ordering seeds **116189 for tuning** and **127081 for held-out**. Record the complete order. All repetitions of a case use identical content and losses but independent new sockets, objects, caches, and fitted state.

Before each batch, run one two-handoff healthy unshaped functional warmup for each participating policy on distinct 64-character artifacts with seeds beginning at 138001. Reset all state afterward. Warmup measurements do not select parameters or alter the grid.

Each batch has a separate **180-second execution budget**, including warmup and post-run audits. Finish the current attempt if the budget is reached, then record every unstarted cell as missing due to budget. Use socket timeouts and retain driver/setup, receiver, framing, validation, and timeout failures with the observed partial counters and error stage. Never silently retry or replace a failed attempt. Incomplete coverage prevents the corresponding success gate.

Feasibility before outcomes: the tuning grid has 324 runs with 54 handoffs across its three workload templates; only half use pacing/holdoff. Its mandatory base reply holdoff is about 35 seconds, with additional loss replies and paced bytes expected to keep the batch below three minutes. The held-out grid has 360 runs and 6,570 handoffs. Its base imposed reply holdoff is **43.8 seconds**; even treating all 19 eligible loss positions across its workload templates as actual misses adds at most **11.4 seconds** of requested holdoff across policies/repeats. A conservative planning allowance of payload plus 4,000 framed bytes per handoff gives about **80 seconds** of requested pacing across the limited profiles. These are planning bounds/allowances, not simulated or measured timing results; local serialization, scheduling, and test overhead can still exhaust the budget. The predeclared response is incomplete coverage, not shrinking the grid after seeing outcomes.

## Freeze order and intended artifacts

1. Publish this design before implementation timing outcomes.
2. Add a new `latency_experiment.py` and focused tests, leaving prior frozen code/results unchanged. Correctness tests may exercise tiny separate socket fixtures; they must not run the tuning or held-out grid or guide parameter changes.
3. Review actual receiver reconstruction, information boundaries, common-codec equivalence, timing boundaries, nonnegative-fit edge cases, action replay, framing/pacing accounting, setup-failure retention, and object-cache isolation. Publish the reviewed source and hashes before tuning.
4. Run the complete tuning grid once. Publish all tuning observations and the deterministic selection before held-out execution.
5. Run the complete held-out grid once without retuning. Publish complete observations, failures, and a measured report regardless of the gates.

Planned commands, to be implemented only after design review:

```bash
PYTHONPATH=src python -m handoff_lab.latency_experiment tune --output results/latency_selection.json
PYTHONPATH=src python -m handoff_lab.latency_experiment evaluate --selection results/latency_selection.json --output results/latency.json
```

Each command will produce a readable summary and deterministic-metadata gzip containing the full report, with recorded checksums. The gzip's byte digest identifies the actual timing report; timings themselves are not expected to reproduce bit-for-bit. A future `docs/latency_results.md` will distinguish shared codec improvements, policy effects, imposed transport behavior, and unresolved limitations. No outcome, success, or novelty is claimed by this design.
