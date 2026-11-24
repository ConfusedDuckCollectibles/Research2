# Real-wire controlled transport v1: measured results

**The recent re-probing policy was never the fastest median among the four tested policies. All 216 runs nevertheless completed with exact receiver reconstruction, so this is a measured negative performance result rather than a correctness failure.** The simple cache/delta baseline was fastest in 11 of the 18 cases; always-inline was fastest in the other 7.

The recent policy beat cache/delta in three deliberately shaped loss/recovery cases, but always-inline was faster in all three. A comparison against only the cache baseline—or only the older online policy—would therefore overstate the result.

## Frozen provenance and real traffic

The [design](network_design.md) was published in [commit c557b458](https://github.com/ConfusedDuckCollectibles/Research2/commit/c557b45892f09d12d94deb10d74e91c112d4427a), and the independently reviewed implementation and tests in [commit cbffe88c](https://github.com/ConfusedDuckCollectibles/Research2/commit/cbffe88cabba6d57963231bc19eae6a362818941), before the timing grid was run. The recent policy retained its previously selected window 4 / re-probe interval 4. No policy or profile was retuned after timing outcomes.

The run finished in **52.92 seconds**, within the declared 180-second budget. It completed all **216 planned runs**, covering **3,456 handoffs**, **7,704 actual frames**, and **20,767,896 framed application-stream bytes** across both directions. There were no failed or missing cells.

Each frame crossed an actual loopback TCP connection and contained a 4-byte length prefix plus real UTF-8 protocol JSON. The receiver reconstructed artifacts from received inline values, cache references, valid deltas and actual recovery responses. It received no expected fixture values through another channel. Both endpoints' byte counts and stream hashes reconcile, as do the receiver's reconstructed values and content digests.

Evidence is retained in the [readable summary](../results/network.json) and [complete compressed observations](../results/network_full.json.gz). The complete report contains every individual run, handoff accounting record, endpoint counter, requested/observed wait, implementation hash and output digest.

## What was imposed

The unshaped profile adds no delay or pacing. The other two profiles apply a **5 ms or 20 ms holdoff before each receiver GET/ACK**, plus a nominal maximum of **1,000,000 framed bytes/second per outgoing direction**. Real bytes are paced in chunks of at most 1,024 bytes. All waits happen before transmission, including the final ACK.

These are controlled application-level service/link conditions on localhost. The holdoff is not an observed WAN RTT. The byte counts include real application frame prefixes; TCP/IP/Ethernet headers and retransmissions are not captured. Timing includes the imposed waits, real transfers, encoding/decoding, receiver reconstruction, policy work and instrumentation.

The completion timer starts before capability transmission, after TCP connection establishment and policy initialization, and stops after the final ACK validation and policy update. Connection setup and teardown are recorded separately. Fixture values and initial content hashes are prepared outside the timer with **fresh Artifact objects for every run**; snapshot serialization caches do not carry across policies or repetitions. Source message/recovery encoding occurs on demand inside the timed work.

## All 18 cases and all four policies

Every case has 16 handoffs and one changed field at events 4, 8 and 12. The string payload sizes below exclude JSON/protocol overhead. Loss schedules are receiver-only: healthy; one loss before event 1; or losses before events 1–9 followed by recovery from event 10.

Each cell shows **median completion milliseconds**, followed by the three individual measurements in repetition order 0 / 1 / 2. Bold marks the lowest median in that row. These are descriptive samples, not statistical significance claims.

### Unshaped loopback

| String payload | Receiver loss | Inline | Cache/delta | Original online | Recent re-probing |
| --- | --- | ---: | ---: | ---: | ---: |
| 2,048 chars | Healthy | **7.957** (9.857 / 7.957 / 7.596) | 9.150 (11.640 / 8.524 / 9.150) | 19.079 (19.079 / 18.566 / 22.366) | 18.122 (18.122 / 16.554 / 18.674) |
| 2,048 chars | One early loss | **7.957** (7.957 / 8.462 / 7.463) | 10.576 (10.576 / 11.098 / 9.538) | 22.002 (18.926 / 26.027 / 22.002) | 21.344 (21.344 / 37.092 / 18.749) |
| 2,048 chars | Loss, then recovery | **8.344** (7.761 / 8.344 / 9.812) | 11.775 (10.097 / 13.569 / 11.775) | 22.122 (19.353 / 22.122 / 22.420) | 17.443 (22.895 / 17.443 / 17.269) |
| 16,384 chars | Healthy | 16.110 (28.088 / 15.547 / 16.110) | **15.770** (19.305 / 14.698 / 15.770) | 39.127 (38.369 / 41.337 / 39.127) | 31.536 (31.536 / 31.892 / 28.521) |
| 16,384 chars | One early loss | 17.019 (15.717 / 19.809 / 17.019) | **14.485** (13.481 / 14.485 / 17.007) | 40.548 (42.717 / 39.393 / 40.548) | 28.009 (27.093 / 28.009 / 29.322) |
| 16,384 chars | Loss, then recovery | **14.306** (13.183 / 14.968 / 14.306) | 19.174 (17.301 / 19.174 / 20.107) | 38.008 (38.008 / 38.466 / 34.540) | 31.282 (27.737 / 31.282 / 32.711) |

### 5 ms receiver-reply holdoff + 1 MB/s pacing

| String payload | Receiver loss | Inline | Cache/delta | Original online | Recent re-probing |
| --- | --- | ---: | ---: | ---: | ---: |
| 2,048 chars | Healthy | 141.415 (141.415 / 142.389 / 141.203) | **106.164** (106.164 / 156.836 / 105.674) | 116.888 (115.610 / 118.680 / 116.888) | 114.251 (111.913 / 114.251 / 114.480) |
| 2,048 chars | One early loss | 143.432 (145.327 / 143.432 / 141.740) | **119.886** (120.185 / 119.886 / 115.822) | 159.856 (158.859 / 160.792 / 159.856) | 130.757 (130.757 / 132.254 / 129.285) |
| 2,048 chars | Loss, then recovery | **139.840** (139.149 / 139.840 / 142.940) | 181.193 (181.193 / 183.026 / 181.082) | 158.248 (158.248 / 157.268 / 160.299) | 160.914 (160.297 / 163.639 / 160.914) |
| 16,384 chars | Healthy | 402.814 (402.814 / 403.543 / 401.123) | **129.519** (129.519 / 133.794 / 129.289) | 151.763 (148.376 / 153.022 / 151.763) | 141.859 (141.859 / 142.275 / 140.403) |
| 16,384 chars | One early loss | 402.630 (401.556 / 405.832 / 402.630) | **153.258** (153.258 / 153.027 / 153.367) | 439.067 (439.067 / 439.881 / 432.117) | 224.741 (224.741 / 224.553 / 226.137) |
| 16,384 chars | Loss, then recovery | 412.587 (423.523 / 402.096 / 412.587) | **354.747** (365.991 / 352.549 / 354.747) | 447.346 (480.320 / 434.569 / 447.346) | 392.922 (420.977 / 378.220 / 392.922) |

### 20 ms receiver-reply holdoff + 1 MB/s pacing

| String payload | Receiver loss | Inline | Cache/delta | Original online | Recent re-probing |
| --- | --- | ---: | ---: | ---: | ---: |
| 2,048 chars | Healthy | 382.018 (382.018 / 381.535 / 383.525) | **348.148** (355.259 / 346.926 / 348.148) | 358.055 (358.055 / 359.156 / 357.937) | 359.010 (359.010 / 361.378 / 355.928) |
| 2,048 chars | One early loss | 384.083 (381.660 / 390.204 / 384.083) | **372.294** (372.294 / 371.470 / 416.552) | 428.832 (428.832 / 418.670 / 441.410) | 386.278 (386.278 / 386.463 / 385.026) |
| 2,048 chars | Loss, then recovery | **383.883** (381.419 / 392.835 / 383.883) | 557.162 (558.520 / 555.761 / 557.162) | 420.006 (421.680 / 420.006 / 414.835) | 448.688 (447.751 / 464.570 / 448.688) |
| 16,384 chars | Healthy | 643.510 (643.695 / 639.544 / 643.510) | **371.803** (371.803 / 370.710 / 373.915) | 394.789 (411.736 / 389.103 / 394.789) | 390.533 (387.266 / 390.533 / 393.834) |
| 16,384 chars | One early loss | 649.631 (678.007 / 649.631 / 643.287) | **412.683** (412.683 / 412.089 / 413.799) | 687.317 (711.563 / 687.317 / 686.699) | 480.707 (480.707 / 480.602 / 482.441) |
| 16,384 chars | Loss, then recovery | **643.265** (642.090 / 650.654 / 643.265) | 732.043 (741.986 / 732.043 / 725.465) | 691.394 (689.729 / 693.311 / 691.394) | 672.688 (675.253 / 671.006 / 672.688) |

## Conditional wins do not establish a general improvement

The predeclared primary statistic is the **median of paired per-repetition completion-time ratios**, not the ratio of the two median times in the tables. Because these operations are different, a percentage below need not equal the percentage obtained by dividing the rounded table medians.

Recent re-probing has a paired median advantage over cache/delta in only these three cases:

| String payload and loss schedule | Imposed profile | Recent reduction versus cache/delta | Paired recent/cache ratios, repeats 0 / 1 / 2 |
| --- | --- | ---: | --- |
| 2,048 chars, loss then recovery | 5 ms holdoff + 1 MB/s | 11.14% | 0.8847 / 0.8941 / 0.8886 |
| 2,048 chars, loss then recovery | 20 ms holdoff + 1 MB/s | 19.47% | 0.8017 / 0.8359 / 0.8053 |
| 16,384 chars, loss then recovery | 20 ms holdoff + 1 MB/s | 8.34% | 0.9101 / 0.9166 / 0.9273 |

All three repetitions favor recent over cache/delta within each of these cases. However, **always-inline has the lower median completion time in all three**, and recent is slower than cache/delta on the paired median statistic in the other 15 cases. It is never the fastest of all four median times in the complete grid.

The measured counters explain the conditional result. In the sustained-loss/recovery schedule, cache/delta incurs 9 recovery exchanges; recent incurs 3; inline incurs none. The 2,048-character case transfers 31,963 framed bytes under cache/delta, 36,097 under recent and 42,010 under inline. At 16,384 characters the respective counts are 178,011, 222,465 and 271,386. Recent trades extra data for six fewer additional GET/recovery-response round trips; all policies still receive the same 16 final acknowledgments. Inline removes the remaining recovery penalty at a larger byte cost. Under the tested shaped conditions, that final tradeoff still favors inline whenever recent beats cache/delta.

On healthy shaped connections, cache/delta is faster while transmitting the same protocol bytes as recent; the recent controller's additional selection/observation work remains overhead. The raw policy elapsed measurements are retained separately. An adaptive controller cannot be credited with benefits already delivered by simple cache reuse.

## Validation and limits

The **82-test suite** passed before measurement, including partial-frame reads, real TCP reconstruction, stale/missing delta-base recovery, malformed messages, actual pre-send pacing, reply-count holdoff accounting, prefix invariance, hidden-state boundaries, fresh object caches and setup-failure reporting.

A post-run audit verified all 216 attempts, all 3,456 reconstructed output digests, both endpoints' stream counts/hashes, prefix bytes, every recovery, and all declared pacing/holdoff budgets. Protocol bytes and action counts are deterministic across repetitions and transport profiles for a given workload/policy. The earlier adaptive and recovery reports retain their original hashes.

The host reported CPython 3.12.14 on Linux x86-64 with 9 logical CPUs. Exact platform and monotonic-clock metadata are in the result summary. Three repetitions give a small timing sample; thread scheduling, host activity, Python overhead and local TCP behavior remain relevant. Small differences should not be treated as statistically established rankings. No actual WAN, LLM inference, agent task-quality, API price or production-throughput result was measured.

The study shows that the byte/exchange tradeoff can change measured completion time under declared imposed conditions. It **does not show that the tested recent policy is preferable to the simplest competing strategies**, and it is not a breakthrough or universal speed improvement.

## Proposed next gate — not executed

A next experiment should optimize and charge the actual completion-time objective, including controller computation, rather than optimizing bytes and assuming that time follows. It should compare against **both** always-inline and cache/delta on fresh source seeds, phase lengths and imposed profiles. Any additional observations or probes must be charged, and the controller must use current/past information rather than future phase labels.

Its design, parameters and success criteria should be frozen before execution. A useful advance would require an advantage over those strong simple alternatives in a declared operating region while making healthy-case and worst-case costs explicit. No such follow-up experiment was run as part of this study.

## Reproduce this fixed grid

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m handoff_lab.network_experiment --output results/network.json
```

The command performs four separate functional warmups, then the 216 fixed measurements in reproducibly randomized order. It writes a readable summary plus a complete gzip report with checksum. Re-running will produce new timing values; the fixture content, ordering, source policies and protocol byte counts remain reproducible.
