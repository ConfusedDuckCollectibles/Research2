# Frozen design: real-wire controlled transport study v1

This design is written before any timing outcomes from the study are run. It preserves the completed recovery experiment, its parameters and its negative results.

## Question

Does the previously measured tradeoff between artifact bytes and protocol exchanges improve actual completion time when real validated messages cross a local socket under explicitly imposed service/link conditions?

This is an **engineering study of a loopback TCP implementation with controlled application pacing and reply holdoff**. It is not a WAN measurement, a production agent workload, an LLM inference benchmark or evidence of a scientific breakthrough.

## Frozen comparison

Compare exactly four policies:

- `inline`: send every artifact in full.
- `cache_delta`: the strong direct-cache baseline, selecting inline/reference/valid delta from the last acknowledgment.
- `original_online`: the previously published cumulative-loss estimator.
- `recent_reprobe`: the previously selected recent-history policy with **W=4 and K=4**.

Do not retune any policy, optimize one implementation based on timing outcomes, or add a current-cache-status channel. All policies use the same sender/receiver implementation, framing, hashing, receiver cache, acknowledgment history and recovery interface.

## Real transport and payload correctness

Use an actual IPv4 TCP connection bound to **127.0.0.1** on an ephemeral port. Enable `TCP_NODELAY` at both endpoints. A receiver worker handles the peer connection. No external network service or paid resource is used.

Each frame is a **4-byte unsigned big-endian payload length followed by the exact UTF-8 JSON payload**. Reads must handle partial prefixes and payloads, reject oversized/malformed frames, and not assume one read equals one frame.

Send the actual serialized protocol messages: capability metadata; inline/reference/delta handoffs; receiver-originated recovery requests; sender-originated snapshot/delta recovery responses; and receiver-originated acknowledgments. Do not send dummy buffers whose lengths are copied from the previous simulator.

The receiver obtains artifact content only from these frames. It decodes each envelope or transfer, checks reference identity and digest, applies a delta only to a valid base, and requests recovery when its cache is missing. Before acknowledging, it must reconstruct and hash the actual resulting JSON object. After each run, the harness additionally compares the receiver's reconstructed values with the sender's intended values. Expected source content is not supplied to the receiver as a side channel.

The sender's policy receives only the current artifact, past sender state, the last validated acknowledgment and the observed recovery outcome. Hidden cache loss executes in the receiver worker immediately before processing the specified handoff. The sender does not receive the receiver's cache object, loss flag, future schedule or final session length through the policy interface.

## Workloads fixed before execution

Use delta-capable direct caching throughout. Every run has **16 handoffs**, numbered 0–15, with an artifact split into 16 fields. Change one field at events 4, 8 and 12. Reuse the existing deterministic artifact generator, but with fresh source seeds and these new payload sizes:

| String payload characters | Source seed |
| ---: | ---: |
| 2,048 | 90571 |
| 16,384 | 90583 |

The same artifact sequence is used across policies, repetitions and loss schedules for a payload size. Protocol IDs, hashes, acknowledgments and framing are additional transmitted bytes.

Use three receiver-only loss schedules:

| Schedule | Receiver behavior before a handoff |
| --- | --- |
| healthy | Never clear a retained cache. |
| single_loss | Clear the cache before event 1 only. |
| loss_recovery | Clear it before events 1–9 inclusive; preserve it from event 10 onward. |

These schedules are absolute-index rules, independent of policy actions or measured time. A short-prefix validation must produce the same decisions as the corresponding prefix of a longer run. There is no random loss selection based on which policy is active.

## Imposed transport profiles

| Profile | Receiver reply holdoff | Per-direction application pacing |
| --- | ---: | --- |
| unshaped | 0 ms | No added pacing |
| holdoff5_rate1m | 5 ms before each receiver reply | 1,000,000 framed bytes/second nominal maximum |
| holdoff20_rate1m | 20 ms before each receiver reply | 1,000,000 framed bytes/second nominal maximum |

A receiver reply is a recovery request or an acknowledgment. Apply the declared holdoff **once per reply**, including an acknowledgment after recovery. Do not call this value WAN RTT: it is an explicitly imposed receiver-side service delay.

Pacing applies independently to both outgoing directions, including the 4-byte prefix. For limited profiles, send real frame bytes in chunks of at most **1,024 bytes**, using monotonic deadlines proportional to bytes sent. Do not grant accumulated idle-time credit that permits an entire large frame to bypass its pacing budget. Record requested pacing/holdoff and observed waiting time. OS scheduling and local processing may make the realized rate slower than the nominal cap.

The delay and pacing are real waits inside the measured transfer. They are deliberately imposed conditions, not observed characteristics of a real WAN. Unshaped results measure this host's loopback and implementation overhead. No TCP/IP/Ethernet packet-header or retransmission-byte claim will be made: the exactly counted bytes are the real application stream, including its physical 4-byte frame prefixes.

## Timing boundary and counters

Record TCP connection setup separately. Start the main completion timer immediately before sending the capability frame, after connection establishment and policy initialization. Stop it after the final validated acknowledgment and the policy's final observation update. This includes capability transfer, all payload serialization/decoding work occurring during the run, policy selection/observation, imposed waits, socket transfer, recovery, receiver reconstruction and acknowledgment handling.

Close sockets and join the receiver outside the completion timer; record teardown separately. Post-run comparison of already reconstructed receiver values with the sender's expectations is a correctness audit, not additional transfer time. Do not exclude individual policies' parsing, hashing or recovery work from their completion boundary.

Record at least:

- Measured completion seconds, setup seconds and teardown seconds.
- Sender policy decision/observation elapsed seconds, separately from total completion time.
- Actual payload bytes, **4 × frame-count** application framing bytes, and total framed stream bytes in both directions.
- Counts of data frames, recovery requests/responses and acknowledgments.
- Requested and observed pacing/holdoff waits per endpoint.
- Cache misses, forced re-probes, action counts and exact reconstruction outcome.

Successful endpoint counters must reconcile: sender bytes sent equal receiver bytes read, and vice versa. Count the complete capability frame and all replies. A failed or timed-out run remains visible and cannot be replaced silently by a successful retry.

## Order, repetitions and runtime budget

The complete measured grid is **2 payloads × 3 schedules × 3 transport profiles × 4 policies × 3 repetitions = 216 runs**. There are no tuning runs.

Use repetition as the outer block. Shuffle the 18 workload/profile cases within each repetition, then shuffle the four policies within each case, using a fixed ordering generator seeded **90601**. This interleaves conditions and avoids placing one policy consistently first. Record the realized execution order and repetition IDs.

Before measured runs, perform one unreported two-handoff functional warmup for each policy, using a separate tiny healthy artifact and the unshaped profile. Do not use warmup timing to change parameters or the grid. Unit tests may use separate small fixtures to verify real socket framing, payload correctness, hidden-state boundaries and charged exchanges.

The anticipated measured runtime is under **two minutes**, with a predeclared **180-second total execution budget**. If the budget is exceeded, finish the current run, stop before starting another, and report incomplete coverage. Do not shrink or alter the grid after inspecting outcomes. Use a per-socket timeout to prevent hanging after a protocol failure. Connection/setup failures and missing cells must be reported.

## Analysis fixed before results

Report all four policies for every workload/profile case. Primary comparisons are paired completion-time ratios for `recent_reprobe` against the strong baseline and the original policy, within the same payload/schedule/profile/repetition. Show the three individual measurements and their median; do not average unlike profiles into a universal speed claim.

Report healthy controls and unfavorable cases alongside favorable loss/recovery cases. The byte and reply counters should explain whether a time difference arises from more data, more exchanges, policy work or imposed waits. Do not subtract imposed waits and then present the remainder as a separate observed network improvement.

Three repetitions provide a small descriptive timing sample, not statistical proof. Host load, thread scheduling, Python serialization and local TCP behavior remain possible sources of variation. If a comparison is small relative to repeat variability, say so. If any run fails correctness, do not claim a timing advantage for that run or silently remove the failure from its case.

The useful outcome is a reproducible, narrowly qualified engineering comparison under declared conditions. Faster results in one imposed profile do not imply faster real agents, lower API bills or a new scientific discovery.

## Freeze order

1. Publish this design before measured outcomes.
2. Implement the real framed transport and pass focused correctness tests without examining the study grid's timings.
3. Record implementation/design hashes and the frozen W4/K4 policy provenance.
4. Run the complete fixed timing grid once. Preserve negative outcomes and failures.
5. Write a measured report and complete machine-readable observations separately from prior studies.

Planned command: `PYTHONPATH=src python -m handoff_lab.network_experiment --output results/network.json`. The command will write a readable summary and a complete compressed observation report. Existing recovery implementation and results remain unchanged.
