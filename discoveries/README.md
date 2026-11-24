# Experimental findings

These are reproducible observations, including negative results. This index does not claim that the underlying methods are novel.

| Study | Finding | Evidence |
| --- | --- | --- |
| Adaptive transport | Savings depend on topology; a strong healthy direct-cache baseline removes the apparent additional byte gain | [Report](../docs/adaptive_transport.md) |
| Cache recovery | Recent history and re-probing improve the old policy, while the strong baseline remains cheaper in aggregate bytes | [Report](../docs/recovery_results.md) |
| Actual TCP transfer | Every transfer is correct, but cache/delta or inline is fastest in every tested case | [Report](../docs/network_results.md) |
| Causal latency control | All 360 held-out sessions are correct, but fitted control is 2.02% slower than the selected miss guard and 3.92% slower than cache/delta; independent accounting audit passed | [Report](../docs/latency_results.md) |

Read [continuation instructions](../docs/continuation.md) and the machine-readable [checkpoint](../research_state.json) before starting new work.
