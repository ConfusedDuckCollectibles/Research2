# Held-out execution host disclosure

Recorded before any held-out timing outcome on 24 November 2026 (America/Chicago).

The local executor disconnected after the complete tuning study and its independent audit. The held-out benchmark never started locally: the attempted environment check was rejected before execution. The complete tuning summary and identical raw archive were published at `eb6f9966260aec784f8eaa9ab98e3d0761f614f3` before this note.

The held-out batch will execute **once** on a clean standard GitHub Actions CPU runner. All five policies and all 360 planned sessions will run together on that runner in the original interleaved order. There will be no local replacement run, retuning, parameter sweep or selective retry afterward.

## What is fixed

The design at `dac8aa0837ed78bc7e9688433e15bc2fef777a3f`, source at `fb2e4aa3e1a76705bd8fc4aaa05d8c293588574a`, all seven recorded source/design hashes, selected model (lambda 0.5, K=4), selected miss guard (K=4), and selected global simple baseline (`miss_guard_K4`) remain fixed. The actual loopback TCP transport, complete case grid, 180-second batch budget, three interleaved repetitions, packet accounting, failure/missing-cell retention, paired-ratio analysis, and all five engineering gates remain unchanged. No intentional CPU-heavy work will run concurrently inside the measurement job. The recorded environment will identify Python, platform, CPU count and clock behavior.

The original design does not require the same tuning and evaluation host. This note therefore records an execution condition, without editing the frozen design or weakening its checks. A separate wrapper/workflow will perform environment/provenance checks, invoke the unchanged CLI, preserve its complete outputs, and publish those bytes without interpreting them as a success.

## Interpretation

Tuning occurred on the workspace host (Python 3.12.14, Linux 6.18.57; full metadata is in the tuning report). Held-out evaluation occurs on another host. Selection transfer across hosts is an additional generalization condition and a limitation. Do not compare aggregate elapsed time between the tuning and held-out batches as a speedup. The prespecified comparisons pair policies within the held-out runner and case. Standard runner variability and the small number of timing repetitions still limit claims about other machines or production networks.

No held-out result is asserted by this note.
