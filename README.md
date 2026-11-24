# Handoff Lab

A runnable research prototype for **typed agent handoffs, checked artifact references, and versioned shared state**. The question is whether a communication format and a state-reuse strategy reduce total work while preserving what a receiving agent needs.

## Latest measured outcome: fitted latency control did not beat strong baselines

The frozen follow-up completed **360 of 360 held-out TCP sessions with exact receiver reconstruction**. The controller fitted reply and byte costs from past observations, with its own computation and recovery traffic charged to completion time. It was **2.02% slower than the tuning-selected miss guard** and **3.92% slower than simple cache/delta** on the predeclared equal-case paired metric. Three of five engineering gates failed.

All 324 tuning sessions and the selected settings were published before the single held-out run. An independent read-only reconstruction and accounting audit passed **196,646 checks without invoking either benchmark again**. The full report includes every case, all paired model/guard ratios, failures, traffic, and the disclosed change from the tuning host to a GitHub CPU runner.

Read the [complete latency report](docs/latency_results.md), [frozen design](docs/latency_design.md), [pre-outcome execution disclosure](docs/latency_execution_amendment.md), [summary](results/latency.json), [complete observations](results/latency_full.json.gz), and [audit receipt](results/latency_audit_receipt.json). This is a negative result for the tested controller, with no production-network, LLM speed, or breakthrough claim.

The preceding [216-run real-wire study](docs/network_results.md) also found that a simple cache or inline policy had the fastest median in every tested case. The later study above directly charged a completion-time objective and retained stronger comparisons.

The first experiment is deliberately small and reproducible. **It does not establish a model speedup or a new universal AI language.** Structured packets still contain ordinary text. They make task identity, operations, references and revision checks explicit; they do not transfer neural hidden states.

## Companion research: information and numerical precision

[Research3's audited coefficient-packet study](https://github.com/ConfusedDuckCollectibles/Research3/blob/5490fe5431e528d979c87dbad4f556d3bbf3b1f6/docs/coefficient_results.md) completed 720 correct TCP exchanges and 300 numerical diagnostics, but failed both performance criteria. Its compact message helped under imposed bandwidth limits; the strongest optimized Gram control captured almost all of that benefit, while shared-table cold setup was expensive. This reinforces the need for complete cost accounting and strong comparisons across both projects.

The separately reviewed [information-recovery note](https://github.com/ConfusedDuckCollectibles/Research3/blob/b9f3265625eaae321e4f3b3c2a105487a4a1b6ca/docs/statebridge_information_recovery.md) characterizes what an aligned latent prefix preserves in exact arithmetic when its reference is known. It distinguishes mathematical recoverability from finite-precision usability and task performance. These are companion findings; they do not change this repository's negative controller result or establish a breakthrough.

## What works

- A versioned envelope with five operations: request, result, update, question and error.
- Immutable artifact revisions, SHA-256 content checks and explicit missing-reference failures.
- Atomic top-level field deltas, with stale-base and conflicting-operation rejection.
- A deterministic audit decoder that displays the actual resolved data.
- A four-condition benchmark that separates representation from state reuse.
- An opt-in HTTP runner for a Chat Completions-style endpoint, recording observed usage, request latency, answer correctness and failures.
- A separate six-task numeric-function repair assay with acceptance cases and a bounded expression interpreter.
- A systematic cache/admission experiment with 342 tuning workloads, 936 held-out workloads, strong direct-cache baselines, and cache-loss stress tests.
- A separately frozen recovery experiment with recent-history estimation, bounded re-probing, and explicitly charged current-status feedback.
- A real framed loopback TCP implementation with receiver-validated payloads, declared byte pacing/reply holdoff, and a frozen 216-run timing study.
- A causal completion-time controller, 324-run tuning study, 360-run held-out study, and read-only reconstruction/accounting audit; the controller's negative performance result is retained.

Python 3.10 or later is sufficient. The runtime and tests use the standard library; no model, tokenizer, GPU or API key is needed for the deterministic experiment.

## Reproduce the local experiment

Run from this repository in a POSIX shell:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m handoff_lab demo --output results/demo.json
PYTHONPATH=src python -m handoff_lab benchmark --output results/deterministic.json
```

Optional installation for the `handoff-lab` command:

```bash
python -m pip install -e .
handoff-lab benchmark
```

The checked-in [deterministic report](results/deterministic.json) and [decoded example](results/demo.json) can be regenerated exactly. Local HTTP tests use a fake loopback server. **No real language-model endpoint was run to produce these checked-in results.**

## Adaptive transport: when should an agent cache or send a delta?

The [adaptive transport experiment](docs/adaptive_transport.md) varies artifact size, repeated use, update size, cache capability and deployment. It compares an online admission heuristic with fixed policies that have exactly the same caching/delta capabilities, including inline-once-then-reference without a redundant external store.

On the held-out synthetic grid, online admission reduces total application payload bytes by **18.93% with external-store delta synchronization** and **23.85% with external-store revision caching**, relative to the fixed comparator selected on tuning data. It adds **zero byte savings over the strong direct-cache baseline**. Across independent cache-loss and stop-after-purchase stress cases, it uses **12.60% more bytes** than the tuning-selected fixed comparator.

Those mixed results narrow the engineering target: better admission under uncertain reuse can matter for an external store; ordinary direct caching and deltas already capture the measured gains in the healthy direct-deployment cases. This remains a synthetic byte experiment, with no token, model quality, cost or speed claim.

```bash
PYTHONPATH=src python -m handoff_lab.adaptive_transport \
  --measure-time --output results/adaptive_transport.json
```

See the [readable result summary](results/adaptive_transport.json), [complete compressed observations](results/adaptive_transport_full.json.gz), and [design, accounting, oracles and failure cases](docs/adaptive_transport.md). The new experiment includes setup, store writes, acknowledgments, recovery requests and recovery responses. Its online strategy uses only current/past observations; future-aware comparisons are labeled as oracles.

## Recovery follow-up: fixing the policy's observation trap

The first adaptive policy could stop trying cache references after an early loss and never learn that retention recovered. We froze a [new design](docs/recovery_design.md), published its selected parameters before testing, then evaluated a recent-history policy with bounded re-probing on **720 new held-out grid cells and 16 fixed stress cases**.

The new policy uses **19.33% fewer held-out bytes than the original online policy**, with exact healthy-case byte parity against the strong cache/delta baseline. Recovery segments without successful cache reuse fall from **120 to 6 out of 144**. It still uses **2.88% more bytes than the strong baseline**, while requiring **17.11% fewer logical protocol round trips**. This recovery study counted logical exchanges; the later [real-wire study](docs/network_results.md) separately measured completion time. Model performance has not been measured.

The negatives are included: stress bytes are **5.25% above the strong baseline**, and stress recovery censoring is slightly worse than the original. A separately priced cache-status intervention also fails to beat the strong baseline in aggregate bytes and adds exchanges. See the [measured report](docs/recovery_results.md), [summary](results/recovery.json), and [complete observations](results/recovery_full.json.gz) for the frozen comparisons and limitations.

## Initial result: smaller packets did not imply less total traffic

Three synthetic source-code handoffs each contain four messages and two artifact revisions. Every condition carries the same task information. The English baseline is a concise controlled template with JSON-valued details; it is not a verbose chat transcript.

All 12 fixture/condition combinations round-trip exactly and give the same deterministic receiver summary. These are serialization and reference-resolution checks, not model accuracy measurements.

| Condition | Agent message bytes | Total bytes, no consumer cache | Total bytes, revision cache | Total bytes, delta sync | Full prompt bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| English, inline artifacts | 8,355 | 8,355 | 8,355 | 8,355 | 10,614 |
| Structured, inline artifacts | 8,523 | 8,523 | 8,523 | 8,523 | 10,749 |
| English, shared artifacts | 3,931 | 12,439 | 9,495 | 9,171 | 8,813 |
| Structured, shared artifacts | 4,099 | 12,607 | 9,663 | 9,339 | 8,948 |

Source: [results/deterministic.json](results/deterministic.json). All sizes are **UTF-8 bytes**, not language-model tokens.

The shared reference packets are smaller, but storing and retrieving these small artifacts costs bytes too. Even the delta-sync scenario has more total logical transfer traffic than inline messages in this fixture. Structured syntax is slightly larger than the concise English template. These observations are useful counterexamples to the assumption that a machine-readable format must be more efficient.

The full-prompt column concatenates all four messages for one receiving-model request, includes protocol instructions, and includes the initial store contents and deltas for shared conditions. Shared state reduces repeated text in this particular prompt layout. That does not establish lower token counts, lower billed cost, better task success, or lower latency. If an orchestrator expands every reference back into every handoff, the reported `expanded_handoff_bytes` are exactly the corresponding inline size.

### What the traffic scenarios count

`Total traffic = agent messages + publisher-to-store writes + store-to-consumer reads`.

- **No consumer cache:** fetch every referenced revision on every message.
- **Revision cache:** fetch each distinct revision once per fixture, starting empty.
- **Delta sync:** receive the initial artifact and update stream once, starting empty.

Producer writes and consumer reads are separate transfer legs. The report includes initial setup and update bytes explicitly. It excludes HTTP headers, TLS/TCP framing, storage metadata and filesystem I/O. The full-prompt column is a separate receiving-model context scenario, not a network total or an additional term in the traffic sum.

## Use the protocol

```python
from handoff_lab.protocol import ArtifactStore, Delta, Envelope, attachment, render_audit

store = ArtifactStore()
v1 = store.add("code/pricing", {"source": "if total > 100:", "checks": [99, 101]})
v2 = store.apply(Delta(v1, {"checks": [99, 100, 101]}))

message = Envelope(
    task="fix-pricing",
    op="result",
    sender="investigator",
    recipient="implementer",
    body={"replacement": "if total >= 100:"},
    artifacts=(attachment(v2),),
)

wire = message.encode()
received = Envelope.decode(wire)
print(render_audit(received, store))
```

A reference contains an artifact ID, positive revision number and content digest. Reading an old revision remains valid. Updating from an old revision fails. An inline attachment carries the same reference plus its full checked value, so it can be read without access to the store.

Artifacts are JSON objects. Deltas replace or remove **top-level fields** and are intentionally not RFC 6902 JSON Patch. The package's stable JSON encoding is also not an implementation of RFC 8785 canonicalization. Cross-language hashing would need a specified canonicalization contract before interoperable deployment.

## Run an actual receiving-model experiment

Set the chosen provider's key in an environment variable through your usual secret manager or shell configuration. Then invoke the runner with the **complete** endpoint URL and an available model identifier:

```bash
PYTHONPATH=src python -m handoff_lab model-benchmark \
  --endpoint https://YOUR_PROVIDER/v1/chat/completions \
  --model YOUR_MODEL \
  --api-key-env HANDOFF_API_KEY \
  --extra-body '{"temperature":0,"max_tokens":384}' \
  --repetitions 3 \
  --output results/live-receiver.json
```

This command makes real requests and may incur the provider's usual charges. The example generation options must be supported by the chosen endpoint; unsupported settings are reported as request failures. The runner does not silently retry or change settings.

Each repetition attempts 12 requests: three fixtures across four conditions. Order is reproducibly shuffled within each fixture. The receiver must return an exact JSON summary of the latest artifact and proposed replacement. The expected summary is not inserted into the prompt. This is a **handoff-comprehension assay**, not a measure of independently diagnosing or fixing a repository defect.

The output records the requested and returned model IDs, request hash/body bytes, response bytes, actual request/response latency, returned token-usage object when supplied, correctness, parsing errors and HTTP failures. Missing usage stays missing. API keys and error bodies are not saved. HTTPS is required except for an explicitly enabled loopback test endpoint; redirects are rejected. Live result files are ignored by git by default so they can be reviewed before publication.

## Test actual code-repair responses

A second assay asks the receiving model to **generate a repair**, without putting the reference solution or acceptance cases in its prompt. Six public development fixtures cover clamping, leap years, ceiling division, interval boundaries, an inclusive sum, and numeric distance.

First validate the evaluator offline:

```bash
PYTHONPATH=src python -m handoff_lab.repair_tasks --output results/repair_self_check.json
```

The [self-check report](results/repair_self_check.json) verifies that original buggy functions fail at least one acceptance case and known reference repairs pass. It is not a model-performance result.

To run the repair assay against a configured model:

```bash
PYTHONPATH=src python -m handoff_lab.repair_tasks \
  --endpoint https://YOUR_PROVIDER/v1/chat/completions \
  --model YOUR_MODEL \
  --api-key-env HANDOFF_API_KEY \
  --extra-body '{"temperature":0,"max_tokens":512}' \
  --repetitions 3 \
  --output results/live-repairs.json
```

This attempts 24 requests per repetition: six tasks across four representation/storage conditions. Each request contains a single handoff; shared references are resolved inside the paid prompt, so this assay measures wrapper effects rather than cross-turn cache savings. The investigator is a fixed fixture, not another live model.

Responses must contain one function with a single numeric/boolean return expression. A bounded AST interpreter evaluates an explicitly supported subset; it never passes generated source to `exec` or `eval`. This restriction makes the acceptance check reproducible but excludes most real software tasks. The public fixtures are development checks, not a held-out programming benchmark. No live repair-model run is included in this initial release.

## Research scope

The [research protocol](docs/research_protocol.md) describes the hypotheses, controls, planned larger evaluation and failure conditions. Initial measurements are exploratory development evidence, not a preregistered scientific result.

[Research3](https://github.com/ConfusedDuckCollectibles/Research3) is the companion project investigating learned communication. Keep evidence from the two projects separate: this repository measures explicit protocol/state behavior and provides optional receiving-model assays.

Existing work already addresses agent interoperability and prompt compression. Relevant primary sources include the [A2A specification](https://a2a-protocol.org/latest/specification/), [LLMLingua-2](https://arxiv.org/abs/2403.12968), [JSON](https://www.rfc-editor.org/rfc/rfc8259), and [JSON Patch](https://www.rfc-editor.org/rfc/rfc6902). This prototype does not claim A2A conformance or originality of schemas, caching, references or deltas.

The in-process store is not a durable or concurrent service. Content hashes do not authenticate senders. Envelope and delta containers are revalidated when serialized/applied; only stored revision snapshots have immutability guarantees. The audit record faithfully resolves supplied artifacts, but it does not expose a model's private reasoning. Production authorization, persistence, capability negotiation and deployment are outside this prototype.
