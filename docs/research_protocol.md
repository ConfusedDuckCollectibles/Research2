# Research protocol: representation versus state reuse

## Question and scope

Does explicit structured communication improve agent work after accounting for every message, setup instruction, shared-state write, artifact read and failed attempt?

This repository implements an engineering baseline. It separates syntax from repeated-state elimination before attempting a learned communication scheme. It does not train an encoder, inspect neural activations or demonstrate transfer between hidden representations.

The initial report contains deterministic byte measurements only. Real-model measurements remain an optional next experiment. Neither the initial fixture design nor any future study described here is externally preregistered.

## Factorial design

| Factor | Level 1 | Level 2 |
| --- | --- | --- |
| Representation | Concise controlled English envelope | Compact typed JSON envelope |
| Artifact delivery | Full artifact value in each message | Checked reference plus shared create/delta transcript |

Every arm has the same task IDs, operations, source text, constraints, proposed replacement, regression checks, artifact revisions and content hashes. This prevents a comparison that gives shared-state capabilities only to the structured arm. It also prevents attributing state reuse to syntax compression.

The controlled-English baseline embeds JSON values so that arbitrary quotes, source code, Unicode and nesting can be preserved exactly. Consequently this experiment concerns envelope representation, not all differences between natural language and formal languages. A future benchmark should add a strong human- or model-written concise prose baseline while holding task information fixed.

## Implemented checks

The deterministic fixtures cover a discount boundary, one-based pagination and order-preserving deduplication. Each has four messages and two artifact revisions. The second revision changes the regression-check list; the old revision remains readable. The receiver is required to use the revision attached to the final handoff.

For each of the 12 fixture/condition pairs:

1. Encode and decode the complete message sequence.
2. Reconstruct shared state from the create/delta transcript when applicable.
3. Verify exact equality of every materialized envelope with the original.
4. Produce a deterministic receiver summary and compare with the known expected object.
5. Measure UTF-8 byte sizes for each explicitly named transfer scenario.

Unit tests additionally cover missing artifacts, missing revisions, mismatched digests, stale updates, atomic rollback on invalid removal, duplicate JSON keys, non-finite numbers, lone surrogates, mutated containers, local HTTP responses, HTTP failures and redirect rejection.

These checks establish that this implementation preserves these fixtures. They do not establish that a language model will understand the packets or successfully execute the proposed task.

## Byte accounting

Let M be encoded handoff bytes, W be initial and subsequent publisher-to-store write bytes, and R be consumer-read bytes. Total logical transfer traffic is M + W + R. Inline delivery has W = R = 0 because the artifact content is already included in M.

For shared delivery, three R scenarios are reported independently:

- No cache: sum the full checked attachment size for every reference occurrence.
- Revision cache: sum the full attachment size once per distinct artifact revision.
- Delta sync: sum the initial create and subsequent delta events once.

Each cache begins empty. Writes and reads represent separate network legs. No-store and co-located-store deployments may have different traffic costs; they should be measured under their own explicit topology. Protocol instructions are reported separately and included in model-prompt byte counts. Application payload sizes exclude wire framing and storage metadata.

The prompt scenario sends one receiving-model request per fixture, containing all four handoffs. Shared delivery includes its store transcript once in that request. No artifact content is assumed to reach the model outside its context. This differs from both per-message model calls and an actual tool-retrieval loop; neither is simulated as a free operation.

## Initial observations

The checked-in report shows exact roundtrips in all 12 combinations. Typed envelopes are slightly larger than controlled-English envelopes. Shared references reduce message bytes, while the added store traffic exceeds the inline baseline for these small fixtures, including under delta synchronization.

These are observations from three deliberately small examples. The report is a sanity check that catches accounting errors, not evidence for a general performance ranking. Dataset size, artifact size, reuse frequency, update size, number of consumers and cache topology can change the outcome.

Token counts are null because an authentic tokenizer was not run. Model correctness, model latency and billed cost are also unmeasured in the deterministic report. Do not derive token counts by dividing characters or bytes by a constant. Different serializations must be measured using the actual model tokenizer or provider-returned usage.

## Optional live receiver assay

`model_benchmark.py` calls an explicitly configured Chat Completions-style HTTP endpoint. The prompt requests a strict JSON summary, not arbitrary executable code. This checks retention of required facts and selection of the latest referenced revision.

Generation settings remain the same across arms. The seeded random order changes arm order within each fixture. Record the actual provider-returned model identifier where available; a requested alias is not proof of a fixed model snapshot. Provider caching and load remain uncontrolled and should be considered when interpreting latency.

Every request attempt stays in the denominator. There are zero automatic retries. API errors, invalid API responses, non-JSON answers and semantically incorrect answers remain distinguishable. Strict exact-match validation intentionally rejects code fences and extra fields. Report a more permissive metric separately if one is later introduced.

Returned usage is saved verbatim. Summaries report the number of requests that supply each usage field. A partial token sum must not be compared as though it covers all requests. Billed-cost fields remain null; pricing, cached-input discounts and tool charges are not inferred.

The local HTTP tests use an artificial server with artificial usage numbers. Those numbers test the client and must never be described as real-model benchmark results.

## Planned coding-workflow study

To evaluate the original engineering ambition, extend beyond reading supplied answers:

1. Freeze a larger held-out task set with executable acceptance checks and multiple files where useful.
2. Compare investigator-to-implementer handoffs with equal tool access, context budgets and opportunities to request missing information.
3. Include strong concise-English baselines with the same artifact references and cache capabilities.
4. Count investigator inference, compression/decompression, implementer inference, retrieval, retries and test executions.
5. Randomize and repeat paired tasks; publish task-level outcomes, aggregate uncertainty and all failures.
6. Test at least two independently implemented model backends before making interoperability claims.

Avoid treating public miniature fixture answers as hidden benchmark results. Synthetic acceptance tasks can validate the harness, but representative external tasks and independent replication are needed for broader conclusions.

### Hypotheses to test

- State reuse helps when repeated artifact content exceeds setup, reference, update and retrieval overhead.
- Typed fields may reduce invalid handoffs even when they cost more tokens.
- Shorter surface strings can increase model tokens or decoding errors; compactness alone does not establish an improvement.
- A learned representation should be judged on total compute and task success, including training and adapters, before comparison with text.

These are hypotheses and engineering expectations, not discoveries established by this repository.

## Criteria for a meaningful result

Measure the tradeoff between successful completed work and total cost/latency. A reduction in one payload field is insufficient. A credible claim should survive a held-out evaluation, strong reference-enabled English baselines, all overheads, paired uncertainty estimates and independent reproduction.

Fail the proposed improvement if savings disappear after setup/retrieval, if errors erase the benefit through retries, or if success falls beyond a prospectively chosen tolerance. No arbitrary success threshold has been declared after inspecting these three fixture results.

## Related work and standards

- [A2A protocol specification](https://a2a-protocol.org/latest/specification/): existing agent message/task/artifact interoperability work. This package is a separate experiment, not a conforming A2A implementation.
- [LLMLingua-2, Pan et al.](https://arxiv.org/abs/2403.12968): prior research in task-agnostic prompt compression. Its reported results are not results of this package.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259): JSON data interchange syntax.
- [RFC 6902](https://www.rfc-editor.org/rfc/rfc6902): a standardized JSON patch format. This package's narrow set/remove field delta is a different, deliberately smaller format.

These sources establish relevant existing work; they do not constitute an exhaustive novelty search.
