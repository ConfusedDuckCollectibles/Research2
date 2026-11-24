# Cache recovery v1: measured results

**The frozen recovery policy substantially improves the previous online implementation, but still does not beat the strong cache/delta baseline on total bytes.** It preserves healthy-case byte parity and fixes most observed recovery failures. It trades slightly more data for fewer logical protocol exchanges.

## The design and selection preceded the results

The [design](recovery_design.md) was published in [commit 94e5bfd](https://github.com/ConfusedDuckCollectibles/Research2/commit/94e5bfd0164f60cc7046d6719d8df74d218fa02d). The reviewed implementation, tests and tuning selection were then published in [commit 3300684](https://github.com/ConfusedDuckCollectibles/Research2/commit/33006843ba5bb47a24639ba24943ced81b433a2d) before held-out evaluation.

Tuning selected a recent observation window of **4 attempts** and a forced re-probe interval of **4 eligible opportunities**. All six parameter candidates satisfied the healthy byte-parity constraint. The selected fixed comparator was `cache_delta` for both cache capabilities. No source code, parameter or workload definition was changed after the selection freeze.

Evidence:

- [Tuning selection](../results/recovery_selection.json) and [all tuning observations](../results/recovery_selection_full.json.gz).
- [Evaluation summary](../results/recovery.json) and [complete held-out/stress observations](../results/recovery_full.json.gz).

The evaluation contains **720 held-out grid cells** and **16 stress cells**, using the new sizes, seeds and absolute-index phase schedules specified in advance. The 720 cells are not 720 independent production traces: several share seeds and prefixes. The results are descriptive measurements of the synthetic grid, without significance or real-world distribution claims.

## Held-out comparison

| Policy | Total application bytes | Logical round trips | Recovery segments without successful cache reuse |
| --- | ---: | ---: | ---: |
| Always inline | 60,387,480 | 11,520 | 144 / 144 |
| Strong cache/delta baseline | **35,755,245** | 15,591 | **0 / 144** |
| Inline once, then cache | 35,755,245 | 15,591 | 0 / 144 |
| Original online policy | 45,602,850 | 12,392 | 120 / 144 |
| Recent history + bounded re-probing | 36,786,351 | 12,923 | 6 / 144 |
| Paid current-status feedback | 36,562,734 | 21,120 | 0 / 144 |

The new policy uses **19.33% fewer bytes than the original online policy**, while using **2.88% more than the strong cache/delta baseline**. Its 12,923 logical round trips are **17.11% fewer** than that baseline's 15,591. That is a measured transport tradeoff, not a latency improvement: no real network timing, bandwidth, concurrency or LLM inference was measured.

All 144 deliberately healthy controls have **exact byte equality** between the new policy and the strong baseline. The same equality holds for all **324 cells with no actual cache losses**, including short prefixes of loss schedules that ended before a loss occurred. This byte parity does not mean zero computation overhead: the new policy still performs additional decision work.

The recovery metric is successful use of a reference/delta after a recovery phase begins. Inline delivery always supplies the correct data, so an inline policy's censored recovery segment is not an incorrect result. All delivered artifacts were reconstructed exactly. Here, every reported recovery segment had at least one eligible reuse opportunity.

## Where the change helps—and where it does not

Each schedule below contains 144 held-out cells.

| Schedule | Strong baseline bytes | Original online bytes | New policy bytes |
| --- | ---: | ---: | ---: |
| Healthy | 2,961,894 | 2,961,894 | 2,961,894 |
| Permanent loss | 12,840,774 | 12,106,764 | 12,264,348 |
| Initial loss, then recovery | 6,326,724 | 12,106,764 | 7,028,346 |
| Healthy, then loss, then recovery | 6,234,114 | 6,320,664 | 6,366,204 |
| Intermittent loss, then recovery | 7,391,739 | 12,106,764 | 8,165,559 |

The new policy lowers bytes relative to the original by **41.95%** in initial-loss/recovery cells and **32.55%** in intermittent-loss/recovery cells. It is **1.30% worse** than the original under permanent loss and **0.72% worse** in healthy/loss/recovery cells. Re-probing supplies useful new evidence, but failed probes and delayed adaptation still cost traffic.

The phase totals explain most of the improvement. In recovery phases, bytes drop from **13,918,012** for the original to **5,087,636** for the new policy. The strong baseline still uses only **3,724,602** there. During loss phases, the new policy's **25,375,069** bytes are slightly above the original's **25,361,192**. These phase numbers exclude the one-time capability advertisement; complete workload totals include it.

The six new-policy recovery segments that remain censored all occur in 15-handoff, revision-cache workloads with a mutation every two handoffs. Recovery starts at event 10, leaving only two eligible reuse opportunities before the session ends. That is shorter than the four-opportunity re-probe interval. The result exposes the cost of a bounded but nonzero observation delay.

## Stress results retain the negatives

Across the 16 pre-specified stress cells:

- Strong baseline: **1,076,390 bytes**.
- Original online: **1,321,957 bytes**.
- New policy: **1,132,868 bytes**—**14.30% below the original**, but **5.25% above the strong baseline**.
- Paid status feedback: **1,156,480 bytes**, **7.44% above the strong baseline**.

The new policy has **9 censored recovery segments out of 44**, versus **8** for the original and **0** for the strong baseline. Thus the recovery-segment goal does not improve on every stress family, even though aggregate stress bytes improve relative to the original.

The worst held-out ratio against the hindsight best fixed policy is **1.2712×**. The worst stress ratio is **1.5270×**: a 4,096-character artifact with one early loss uses 36,082 bytes under the new policy, versus 23,630 for the strong baseline. The early miss still causes several full deliveries before re-probing resumes reuse. These outcomes were preserved, not used for another parameter search.

## Paid feedback is not free knowledge

The status intervention pays for a serialized request and reply before potentially useful cache deliveries. That information removes its cache-miss recovery uncertainty under the stated assumption that the cache cannot be evicted between the reply and delivery.

It nevertheless uses **2.26% more held-out bytes** and **35.46% more logical round trips** than the strong baseline. On deliberately healthy controls it adds **19.14% bytes**, because it pays for information that the last acknowledgment already predicted correctly. It has fewer recovery failures but does not dominate the simpler baseline.

## Computation and accounting

The byte totals include common capability metadata, inline/reference/delta handoffs, all recovery requests/responses, acknowledgments, and both status packets when requested. A forced re-probe is an ordinary charged handoff, not a second uncharged channel or a double-counted operation.

A logical round trip is one data/ack exchange plus each required recovery exchange and each explicit status exchange. It is an abstract protocol count. Measured Python decision/observation elapsed time averaged approximately **0.793 ms per handoff** for the new policy, **0.985 ms** for the original and **0.293 ms** for the strong baseline in this run. These timings include this unoptimized simulator's serialization and quotation work, and are not production or inference benchmarks.

The full test suite has **69 passing tests**, including wire validation, charged feedback/re-probing, exact reconstruction, generated-prefix consistency, policy prefix invariance and frozen-selection hash checks. No real model or external API call was made. The previous experiment's code and result files are unchanged.

## What this establishes

The original observation trap is substantially reduced on new frozen test data without a healthy byte penalty. The bundle of recent-history estimation and re-probing remains worse than the strong baseline on aggregate bytes; this study does not separately isolate the two components' effects.

A useful next question is whether fewer protocol exchanges compensate for the extra data and decision work under measured network conditions and real agent traces. That requires a new frozen experiment. The current result is an engineering improvement and a clearer cost tradeoff, not a scientific breakthrough or a universal agent communication language.

## Reproduction

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m handoff_lab.recovery_experiment tune \
  --output results/recovery_selection.json
PYTHONPATH=src python -m handoff_lab.recovery_experiment evaluate \
  --selection results/recovery_selection.json --measure-time \
  --output results/recovery.json
```

For the original study, the code/selection commit occurred between the final two commands. Evaluation checks the design and implementation hashes recorded during tuning. Byte results and parameter selection are deterministic; elapsed times vary by host and load. Each summary links to its complete gzip report and includes the compressed file's SHA-256 checksum.
