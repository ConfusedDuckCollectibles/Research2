# Experiment 2: where caching pays, and whether an online policy adds anything

The initial experiment found that an external store could make packets smaller while increasing total transfer traffic. This experiment varies the conditions behind that result and adds a strong direct-cache baseline. It measures **application payload bytes**, not language-model performance.

## Findings

**The online admission heuristic helps in the external-store grid, but adds no byte savings over the strong direct-cache baseline.** It also loses under important stress conditions. This is evidence for choosing the right deployment and testing cache reliability, not evidence of a new AI language.

Each row below contains 156 held-out workloads. The fixed comparator was chosen on tuning data for that declared deployment/cache capability, then frozen. Its choice was `inline_once_cache` for the two cache-capable external-store groups, `inline` for external-store/no-cache, and `cache_delta` for direct groups. In direct/no-cache, that baseline already acts exactly like inline.

| Deployment and capability | Selected fixed bytes | Online bytes | Online byte reduction |
| --- | ---: | ---: | ---: |
| Direct, no cache | 45,469,228 | 45,469,228 | 0.00% |
| Direct, revision cache | 20,260,152 | 20,260,152 | 0.00% |
| Direct, delta sync | 12,266,592 | 12,266,592 | 0.00% |
| External store, no cache | 45,470,476 | 45,470,476 | 0.00% |
| External store, revision cache | 31,647,980 | 24,100,542 | 23.85% |
| External store, delta sync | 23,623,130 | 19,151,328 | 18.93% |

Source: [machine-readable summary](../results/adaptive_transport.json). The [complete compressed report](../results/adaptive_transport_full.json.gz) retains all tuning scores, held-out observations, stress cases, byte components and timings.

The tuning procedure selected threshold **0.0**. Admission still requires positive reuse credit from previous handoffs, so this does not mean unconditional immediate publication. It means the tuning grid favored the least conservative candidate once prior reuse evidence existed. A stronger claim about rent-versus-buy optimality would not follow from this result.

Against the **hindsight best fixed policy chosen separately for each full workload**, the online totals remain worse in both cache-capable external-store groups: 24,100,542 versus 23,217,240 bytes for revision caching, and 19,151,328 versus 18,114,196 for delta sync. The online policy is not a substitute for knowing the future.

### Failure cases matter

Across 15 stress workloads, online uses **1,441,497 bytes** versus **1,280,140** for the tuning-selected fixed comparator: **12.60% more**. The worst ratio against the best fixed policy for an individual stress workload is **1.9054×**.

In the direct/delta-sync workload with a 50% hidden-loss probability, an early loss makes the simple online estimator stop using the cache, after which it receives no evidence of recovery. It uses 149,456 bytes while the cache/delta baseline uses 78,438. This is an informative failure of the estimator. It was retained rather than used to retune the policy on its test data.

Stopping immediately after admission also hurts. For a stable 4,096-character artifact ending after three handoffs, online uses 14,322 bytes, inline uses 14,060, and the inline-once/cache baseline uses 10,024. Paying setup cost just before work ends is a real risk when the future is unknown.

### Decision work is additional overhead

In the measured Python simulator, direct/delta-sync selection and observation averaged about **1.47 ms per handoff** for online versus **0.47 ms** for the cache/delta baseline. External-store/delta-sync averaged about **0.93 ms** versus **0.16 ms** for the selected inline-once/cache baseline. These include unoptimized Python serialization and cost quotation; they are neither inference timings nor production latency predictions. They show why byte savings must not be advertised as free speed improvements.

## Reproduce

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_adaptive_transport.py -v
PYTHONPATH=src python -m handoff_lab.adaptive_transport \
  --measure-time --output results/adaptive_transport.json
```

The complete run uses 342 tuning workloads, 936 held-out workloads, and independent stress cases. Byte counts, workload generation and decisions are deterministic. Optional decision elapsed times vary with the host and load.

The command writes a readable summary at the requested path and a sibling `adaptive_transport_full.json.gz` containing every observation. The gzip header has a fixed timestamp. The summary includes its SHA-256 checksum and row counts. To inspect all observations:

```python
import gzip
import json

with gzip.open("results/adaptive_transport_full.json.gz", "rt", encoding="utf-8") as stream:
    report = json.load(stream)
print(report["heldout_rows"][0])
```

## Fair comparisons require specifying the deployment

| Deployment | Meaning of a reference | Recovery after cache loss |
| --- | --- | --- |
| Direct cache | The producer refers to content already delivered to this consumer. No external store is required. | Consumer requests a full snapshot or valid delta from the producer. |
| External store | The referenced revision must be published in a separate store before the reference is sent. | Consumer requests the revision from the store. |

The external store has a different availability contract: it can serve a published artifact independently of the producer. This simulator counts the extra transfer legs but does not model outages or assign a numerical value to that availability. Compare policies **within** each deployment. A direct-cache win over an external store is a topology tradeoff, not an adaptive-algorithm improvement.

Each deployment is evaluated with three cache capabilities:

- `no_cache`: discard artifact content after every delivery.
- `revision_cache`: retain the last complete artifact revision.
- `delta_sync`: retain the last revision and accept a checked delta from its immediately preceding revision.

Every policy gets the same capabilities. Inline delivery also seeds the consumer cache, and every successful delivery returns a charged acknowledgment indicating retention. Direct-cache baselines never publish redundant external copies. No-cache direct baselines already choose inline delivery.

## Policies

| Policy | Behavior |
| --- | --- |
| `inline` | Send the full artifact in every handoff. |
| `cache_snapshot` | Use the declared cache deployment, with snapshots for changed content. |
| `cache_delta` | Use the same deployment, with an adjacent-revision delta when its currently measured payload is cheaper. Direct mode can choose inline for a large update. |
| `inline_once_cache` | Send the first artifact inline, then use `cache_delta`. This avoids an unnecessary first fetch and is an especially important external-store baseline. |
| `online` | Use only current/past artifacts and acknowledged cache state. In external-store mode, wait until accumulated observed reuse credit meets a tuned admission threshold, then use delta-capable storage. In direct mode, compare current cache choices while estimating unexpected loss from previous probes. |

Online rent-versus-buy is an established class of caching strategies. This implementation is a simple heuristic, not a new algorithm or a competitive-ratio result. Its threshold is selected from 0, 0.5, 1, 2 and 4 on the tuning workloads only.

The online policy's credit is an accumulated difference between actual inline payload cost and the current cost of a hypothetical already-established cache. The hypothetical cache is constructed from the previous observed artifact, not from future knowledge. Actual entry costs are still charged when the policy admits an artifact. Changes, short sequences and cache loss can make this heuristic wrong.

For hidden loss, the policy estimates a loss fraction from previous reference/delta attempts that were expected to hit. Compulsory cold fetches do not count as unexpected loss. An inline choice supplies no observation about whether the old cache was present. This simple estimator can stop probing after a loss and fail to recognize subsequent recovery; that limitation is exposed by the stress cases, not patched using their outcomes.

## What is charged

Each result includes the sum of:

1. Initial cache/deployment capability metadata.
2. Producer-to-consumer handoff bytes, including IDs, revisions and content hashes.
3. Producer-to-store snapshot/delta bytes, when required.
4. Store acknowledgments.
5. Cache-miss recovery request bytes.
6. Full snapshot or valid-delta recovery response bytes.
7. Consumer acknowledgments.

The receiver must reconstruct the exact intended JSON object and verify its content digest. A missing or stale delta base requires recovery. Cache loss occurs before delivery without notifying the policy; the previous retention acknowledgment can therefore be stale. Recovery is charged in addition to the unsuccessful initial reference or delta.

This is a single-producer, single-consumer, single-artifact simulator. Full snapshots may import a source revision after skipped revisions; deltas require an adjacent checked base. This benchmark-specific snapshot transport does not change the original in-process `ArtifactStore` API.

HTTP/TCP/TLS framing, authentication, content compression, storage I/O, network delays, outages and model-context costs are not simulated. These omissions prevent turning the byte result into a price or latency claim. Optional `decision_elapsed_nanoseconds` records actual Python selection/observation elapsed time, including its serialization and cost quotation work. It is reported separately rather than converted into fictitious byte savings.

## Tuning and held-out workloads

Payloads are deterministic synthetic ASCII strings divided among 16 top-level fields. They control artifact and update size; they are not natural-language tasks or a sample of production agent traffic. JSON metadata and protocol headers make encoded artifact sizes larger than the requested string-payload size.

| Parameter | Tuning values | Held-out values |
| --- | --- | --- |
| String payload characters | 128, 1,024, 8,192 | 64, 512, 4,096, 32,768 |
| Handoffs per workload | 2, 6, 12, 24 | 1, 4, 8, 16, 32, 64 |
| Update interval | Never, every 2, every 6 | Never, every 1, every 4, every 16 |
| Changed fields out of 16 | 1, 4, 12 | 1, 8, 16 |
| Cache/deployment | All six combinations | All six combinations |

Combinations in which the update interval exceeds the workload length are deduplicated as unchanged workloads. Tuning and held-out artifact sizes, sequence lengths and random seeds differ. Stable workloads and one-field updates appear in both as broad behavior classes.

The tuning objective is aggregate byte count, so large artifacts and longer sequences have more influence. The report therefore also gives results separately by deployment/cache capability, per-workload wins/ties/losses, median ratios and worst ratios. These are a synthetic grid, not an estimate of an unknown real-world workload distribution.

## Baseline selection and oracle labels

The implementable fixed comparator is selected on tuning data separately for each declared deployment/cache capability. That selected policy remains fixed during held-out evaluation.

The report also chooses the best fixed policy **after seeing each complete workload**. This is labeled `hindsight_best_fixed_per_workload_bytes`; it is an oracle comparison, not an implementable predictor.

Stress cases additionally enumerate every possible one-time switch from inline to delta-capable caching. Their `single_switch_oracle` sees the entire workload. It finds the best admission time within that restricted policy family, not the globally optimal sequence of arbitrary actions.

## Stress cases

Independent cache-loss workloads use 4,096-character payloads, 32 handoffs, a one-field update every four handoffs, and loss probabilities of 0.1, 0.5 and 1.0. Both cache-capable topologies and both deployments are tested. Loss is sampled from a fixed separate seed and is unknown to the policy until recovery is requested.

The stop-after-purchase stress cases first observe when the frozen online policy admits a stable artifact, then end the workload immediately after that purchase. This is an intentionally adversarial test of unnecessary setup cost, not a random held-out sample. Neither stress family is used to retune the policy.

## Interpretation limits

Do not credit an adaptive policy for gains already achieved by the cache/delta baseline with the same capabilities. Direct caching may already realize essentially all available byte reduction in stable workloads. A positive result against a weaker always-inline policy does not establish an adaptive contribution.

Likewise, a policy that lowers aggregate bytes can still lose badly on a short workload, after a cache loss, or under a different traffic distribution. The complete JSON retains those failures. The next external-validity step is measured real agent traces with a frozen routing policy, including real tokenization, context materialization, latency and task correctness.

Related areas include agent protocols and prompt compression cited in the [original research protocol](research_protocol.md). This experiment has not established novelty relative to existing adaptive caching, admission-control or agent-communication work.
