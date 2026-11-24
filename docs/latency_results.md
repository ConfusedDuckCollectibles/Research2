# Measured results: causal completion-time controller

**The time model did not improve on the strong baselines.** Its primary held-out completion-time ratio to the validation-selected miss guard was **1.020179**, meaning **2.02% more time** on the predeclared aggregate. It was also **3.92% slower than the simple cache policy**. Only the correctness and healthy-overhead gates passed; the three performance gates failed.

All **360 planned sessions reconstructed correctly**, with no failures or missing cells. The fixed batch ran once, completing its batch timer in **78.316 seconds**. These are controlled loopback transport results. They do not establish faster LLM agents, lower token bills, or a new general-purpose communication method.

## Evidence and freeze history

The [design](latency_design.md) fixed the models, tuning/held-out split, profiles, metric, five gates, and failure rules before outcomes. The [execution disclosure](latency_execution_amendment.md) recorded the move to a GitHub CPU runner before any held-out timing. The original study source and design remained unchanged.

| Checkpoint | Commit or identifier |
| --- | --- |
| Design with strengthened miss-guard parity gate | `dac8aa0837ed78bc7e9688433e15bc2fef777a3f` |
| Reviewed implementation before tuning | `fb2e4aa3e1a76705bd8fc4aaa05d8c293588574a` |
| Published tuning observations and selected settings | `eb6f9966260aec784f8eaa9ab98e3d0761f614f3` |
| Pre-outcome host disclosure | `d4fd08c71a90b6bd14772ca799f4770245181e0e` |
| Guarded held-out workflow publication | `99bff23b84f30141d5d5241fed0ec3320080d255` |
| Published held-out outputs | `b57677c3afcad5b68e452b5eea392775e8ba2236` |
| Held-out execution | [Run 45347593386](https://github.com/ConfusedDuckCollectibles/Research2/actions/runs/45347593386), attempt 1; job 138285820298 |

The complete evidence is in [the summary](../results/latency.json), [compressed raw observations](../results/latency_full.json.gz), and [execution provenance](../results/latency_execution.json). The summary contains all three individual completion times and paired ratios for every case. The full report also preserves per-handoff packet metadata, decisions, observations, hashes, wait counters, and timing components.

The complete gzip is **1,009,946 bytes**, with SHA-256 `c203f2d76cc027d06d04d2c669687c1c956e59c4183cac9690c3e0a19ad04e4a`. Its decompressed JSON is **12,299,308 bytes**. The summary SHA-256 is `ef9560c5982aafbead5f76aefd7b37352e6c3120c771c44ccb53575beda31571`.

**Independent audit passed:** [read-only audit run 45349409159](https://github.com/ConfusedDuckCollectibles/Research2/actions/runs/45349409159), job 138291796761, completed **196,646 checks with zero benchmark invocations**. It regenerated sources, updates, loss schedules, execution order, canonical framed streams, byte counts, output digests, selection, paired statistics, and all five gates from the existing reports. It verified the tuning totals of 5,832 handoffs, 14,768,938 framed bytes, 13,168 frames, and 590 GET recoveries; the held-out totals were 6,570 handoffs, 24,517,450 framed bytes, 14,792 frames, and 646 GET recoveries. Replay reused the frozen controller and decoder against reconstructed packet metadata and past observations; those components were not a second independent implementation. The complete retained audit artifact has SHA-256 `c6fdd0096ed81f08b7e66cac26801e8fdc75a59ca945396c55bcc346c4c21d37`. Any separately saved permanent receipt records the successful log and artifact metadata; it is distinct from the complete retained audit artifact. Neither benchmark stage was rerun.

## What was compared

All policies used the same lazy packet codec, real four-byte-prefixed TCP frames, receiver cache, strict decoder, hash validation, and recovery interface. The codec cached canonical content within a session, so each policy paid for the representations it requested without repeatedly rebuilding hypothetical full paths. Every run started with fresh source objects, codec caches, and controller state.

The fixed policies were full inline transmission and a direct-cache policy using references or valid small deltas. The cheap miss guard used cache while trusted, switched to inline after a real GET, and attempted an ordinary cache delivery on every fourth eligible opportunity until a successful probe restored trust. The recent reference policy preserved the earlier W4/K4 byte rule using the common codec.

The proposed controller fitted two nonnegative coefficients from completed transfers: an effective cost per receiver reply and per KiB of framed traffic. It used the last four real cache-attempt outcomes to estimate loss, and a charged ordinary probe after four suppressed opportunities. Tuning selected **lambda = 0.5, W = 4, K = 4**. The selected miss guard was **K = 4**, and it was the one global simple baseline frozen before held-out execution.

The controller received no configured pacing/holdoff, current receiver cache status, loss labels, future source updates, or final session length. Its decisions used current sender-owned packet sizes and past completed observations. Its own quote preparation, selection, fitting, and observation work remained inside the completion timer.

## Primary metric and all five gates

For each of 24 cases, the metric takes the median of the three paired ratios of model time to comparator time. It then takes the geometric mean of those 24 case ratios. Cases have equal weight. This differs from dividing pooled elapsed times or dividing separately aggregated policy scores.

| Gate fixed before held-out timing | Observed outcome | Result |
| --- | --- | --- |
| All planned sessions correct with reconciled traffic and exact reconstruction | 360/360 sessions; no failed or missing cells | **Pass** |
| At least 5% aggregate improvement over the validation-selected simple baseline | Model/guard = **1.020179**, required at most **0.95** | **Fail** |
| Aggregate parity or better against inline, cache, and the selected guard | Model/inline = **0.801760**; model/cache = **1.039237**; model/guard = **1.020179** | **Fail** |
| Healthy-case median paired excess no greater than max(0.5 ms, 5% of cache median) | All six healthy cases met their frozen allowances | **Pass** |
| Worst case no more than 1.25 times the fastest whole-session simple policy chosen in hindsight | Worst ratio of medians = **1.255379** | **Fail** |

The primary comparison is a **direct paired model-versus-guard calculation**. It is not obtained by dividing their separate cache-relative aggregate scores; medians make those operations non-equivalent.

| Comparator | Model/comparator aggregate | Interpretation within this grid |
| --- | ---: | --- |
| Selected miss guard K4 | **1.020179** | Model used **2.02% more time** |
| Cache with deltas | **1.039237** | Model used **3.92% more time** |
| Recent W4/K4 byte rule | **1.039161** | Model used **3.92% more time** |
| Always inline | **0.801760** | Model used **19.82% less time** |

The comparison against inline alone is insufficient: the selected guard, cache policy, and recent byte rule were all faster than the fitted model on their direct aggregate comparisons.

The worst robustness case used 12,288 payload characters, the burst/recovery schedule, 4 ms reply holdoff, and 400,000 framed bytes/second per direction. The model's median was **545.468 ms**, versus **434.505 ms** for the miss guard: **25.54% more time**, just beyond the predeclared 25% bound. The bound was not loosened after observing this result.

## All case medians

These are complete session times in **milliseconds**, including capability transfer, packet preparation, policy computation, all real recovery exchanges, receiver reconstruction, and imposed waits. Connection setup and teardown were recorded separately under the same rule for every policy.

“4 ms / 400 kB/s” means a deliberately imposed 4 ms holdoff before every receiver GET or ACK and a nominal 400,000 framed application bytes/second cap in each direction. “16 ms / 1.2 MB/s” uses 16 ms and 1,200,000 bytes/second. These holdoffs are not measured WAN RTTs.

### 1,536 source payload characters

| Workload | Imposed profile | Inline | Cache | Guard | Recent | Time model |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| healthy | Unshaped | 3.477 | 3.112 | 3.208 | 3.179 | 3.238 |
| healthy | 4 ms / 400 kB/s | 232.525 | 133.491 | 133.686 | 133.495 | 133.460 |
| healthy | 16 ms / 1.2 MB/s | 439.474 | 404.617 | 404.760 | 404.871 | 404.661 |
| short_single | Unshaped | 1.147 | 1.157 | 1.151 | 1.157 | 1.221 |
| short_single | 4 ms / 400 kB/s | 68.223 | 51.754 | 64.366 | 51.670 | 64.388 |
| short_single | 16 ms / 1.2 MB/s | 128.115 | 137.288 | 141.589 | 137.112 | 141.584 |
| burst_recovery | Unshaped | 3.504 | 3.781 | 3.439 | 3.753 | 3.991 |
| burst_recovery | 4 ms / 400 kB/s | 232.700 | 208.357 | 178.410 | 188.089 | 204.642 |
| burst_recovery | 16 ms / 1.2 MB/s | 438.839 | 549.616 | 450.073 | 482.985 | 463.603 |
| intermittent_recovery | Unshaped | 2.673 | 2.746 | 2.701 | 2.798 | 2.897 |
| intermittent_recovery | 4 ms / 400 kB/s | 174.510 | 138.793 | 145.432 | 139.031 | 145.417 |
| intermittent_recovery | 16 ms / 1.2 MB/s | 329.037 | 376.200 | 349.059 | 376.261 | 340.109 |

### 12,288 source payload characters

| Workload | Imposed profile | Inline | Cache | Guard | Recent | Time model |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| healthy | Unshaped | 5.695 | 4.145 | 4.042 | 4.155 | 4.026 |
| healthy | 4 ms / 400 kB/s | 900.057 | 169.203 | 169.384 | 169.218 | 174.378 |
| healthy | 16 ms / 1.2 MB/s | 672.001 | 417.728 | 417.369 | 417.549 | 418.588 |
| short_single | Unshaped | 1.818 | 1.518 | 1.739 | 1.564 | 1.823 |
| short_single | 4 ms / 400 kB/s | 262.644 | 109.222 | 203.312 | 109.323 | 109.382 |
| short_single | 16 ms / 1.2 MB/s | 196.061 | 157.396 | 190.183 | 157.524 | 190.316 |
| burst_recovery | Unshaped | 5.812 | 5.148 | 4.900 | 5.188 | 5.780 |
| burst_recovery | 4 ms / 400 kB/s | 898.961 | 466.096 | 434.505 | 545.639 | 545.468 |
| burst_recovery | 16 ms / 1.2 MB/s | 672.133 | 639.478 | 540.658 | 623.006 | 606.044 |
| intermittent_recovery | Unshaped | 4.439 | 4.104 | 3.977 | 4.017 | 4.469 |
| intermittent_recovery | 4 ms / 400 kB/s | 675.054 | 357.765 | 397.355 | 357.917 | 357.964 |
| intermittent_recovery | 16 ms / 1.2 MB/s | 504.408 | 482.691 | 437.727 | 483.344 | 472.008 |

The smallest numerical median belonged to cache in **8 cases**, the miss guard in **8**, inline in **5**, the recent rule in **1**, and the time model in **2**. The model's two numerical wins were healthy cases with the same reference/delta actions and byte traffic as the other cache policies. Its margins over the best simple-policy medians were only **0.031 ms** and **0.015 ms**. The model's zero-loss fast path did not need to fit costs for these decisions; these tiny timing differences do not demonstrate value from fitting the time model.

## Traffic, recovery, and healthy overhead

The held-out grid contained **6,570 actual handoffs**. Across all policies it transmitted **24,517,450 framed application bytes** in **14,792 frames**, including capabilities, data, GETs, recovery snapshots, ACKs, and every physical four-byte frame prefix.

| Policy | Total framed bytes across its 72 sessions | GET recoveries | Forced ordinary probes |
| --- | ---: | ---: | ---: |
| Inline | 9,840,510 | 0 | 0 |
| Cache | 3,098,214 | 252 | 0 |
| Miss guard K4 | 3,680,064 | 90 | 90 |
| Recent W4/K4 | 3,406,743 | 189 | 27 |
| Time model | 4,491,919 | 115 | 114 |

These are pooled traffic totals for the synthetic grid; they are not the equal-case time metric. Larger payloads and longer sessions contribute more bytes to these sums.

Compared with the selected guard, the model sent **811,855 additional framed bytes**, or **22.06% more**, and incurred **25 additional GET recoveries**. The timing comparison does not isolate arithmetic overhead: the chosen paths also differ in traffic and recovery.

All healthy cache-policy traces had identical framed-byte counts. Their timing differences include controller work and ordinary host/scheduling variation. The largest healthy median paired excess over cache was **4.620 ms** in the larger-payload 4 ms/400 kB/s case; its predeclared allowance was **8.460 ms**. Passing the healthy-overhead gate means the frozen tolerance was met, not that the controller was always faster or free.

## Direct paired ratios against the selected guard

Each value below is model completion time divided by guard completion time for the same case and repetition. Ratios above one favor the guard. The final column is the median of the three ratios, not a ratio of the case medians above.

| Payload chars | Workload | Profile | Ratios by repetition (0, 1, 2) | Median paired ratio |
| ---: | --- | --- | --- | ---: |
| 1536 | healthy | Unshaped | 1.009147, 0.991669, 1.030391 | 1.009147 |
| 1536 | healthy | 4 ms / 400 kB/s | 0.999791, 0.998309, 0.999560 | 0.999560 |
| 1536 | healthy | 16 ms / 1.2 MB/s | 1.000307, 0.994003, 1.005666 | 1.000307 |
| 1536 | short_single | Unshaped | 1.047373, 1.026372, 1.064659 | 1.047373 |
| 1536 | short_single | 4 ms / 400 kB/s | 0.998424, 1.005221, 0.997379 | 0.998424 |
| 1536 | short_single | 16 ms / 1.2 MB/s | 1.000671, 0.999696, 1.000174 | 1.000174 |
| 1536 | burst_recovery | Unshaped | 1.160441, 1.120374, 1.141423 | 1.141423 |
| 1536 | burst_recovery | 4 ms / 400 kB/s | 1.147905, 1.145200, 1.149383 | 1.147905 |
| 1536 | burst_recovery | 16 ms / 1.2 MB/s | 1.030061, 1.030981, 1.030028 | 1.030061 |
| 1536 | intermittent_recovery | Unshaped | 1.072485, 1.115513, 1.079931 | 1.079931 |
| 1536 | intermittent_recovery | 4 ms / 400 kB/s | 0.998395, 1.002026, 1.000571 | 1.000571 |
| 1536 | intermittent_recovery | 16 ms / 1.2 MB/s | 0.977937, 0.974273, 0.974106 | 0.974273 |
| 12288 | healthy | Unshaped | 1.009328, 0.991411, 0.996214 | 0.996214 |
| 12288 | healthy | 4 ms / 400 kB/s | 1.002092, 1.055126, 1.010981 | 1.010981 |
| 12288 | healthy | 16 ms / 1.2 MB/s | 1.000378, 1.002377, 1.002921 | 1.002377 |
| 12288 | short_single | Unshaped | 1.026634, 1.084996, 1.021847 | 1.026634 |
| 12288 | short_single | 4 ms / 400 kB/s | 0.538706, 0.527235, 0.537240 | 0.537240 |
| 12288 | short_single | 16 ms / 1.2 MB/s | 1.000706, 0.997954, 1.001153 | 1.000706 |
| 12288 | burst_recovery | Unshaped | 1.205379, 1.193158, 1.176542 | 1.193158 |
| 12288 | burst_recovery | 4 ms / 400 kB/s | 1.254738, 1.259020, 1.255824 | 1.255824 |
| 12288 | burst_recovery | 16 ms / 1.2 MB/s | 1.125128, 1.119846, 1.120938 | 1.120938 |
| 12288 | intermittent_recovery | Unshaped | 1.182124, 1.101073, 1.207539 | 1.182124 |
| 12288 | intermittent_recovery | 4 ms / 400 kB/s | 0.901203, 0.899964, 0.901600 | 0.901203 |
| 12288 | intermittent_recovery | 16 ms / 1.2 MB/s | 1.101305, 1.080432, 1.068915 | 1.080432 |

## Host change and limits

Tuning ran on the original workspace host. That executor became unavailable before held-out execution; the held-out CLI never started locally. The host change was published before the single GitHub execution, and no local replacement run or selective retry followed.

Held-out execution used **Python 3.12.14**, **four reported CPUs**, and **Linux 7.0.0-1017-azure x86_64 with glibc 2.39** on the declared Ubuntu runner. The recorded monotonic clock resolution was 1 ns; that is a clock property, not the precision or certainty of the performance conclusion. All held-out policies ran together in the predeclared interleaved order on this host, after the full unit suite passed.

Selection transfer across hosts is an additional generalization condition. Host and workload/profile changes occur together, so this study cannot separate their contributions to selection transfer. It also cannot isolate the common codec's optimization from previous studies: the codec, workload grid, and host changed. The 54.394-second tuning batch and 78.316-second held-out batch have different grids and hosts and must not be compared as a speedup.

There are only three timing repetitions per case and two content seeds paired with payload sizes. The 360 sessions are not 360 independent workload samples. Host variability, thread scheduling, Python serialization, and the imposed service model limit generalization. The single-artifact receiver cache, complete-cache-loss model, and short synthetic schedules also omit many deployment behaviors.

The time fit is a small engineering heuristic within an established area of cost-aware caching and communication control; see the primary-source discussion in the [design](latency_design.md). This experiment provides **no novelty or breakthrough claim**. It shows that this frozen controller did not add aggregate value over its strong comparators under the declared conditions. Further development would require a new prespecified experiment and fresh held-out observations; retuning on these outcomes would not turn this evaluation into fresh evidence.
