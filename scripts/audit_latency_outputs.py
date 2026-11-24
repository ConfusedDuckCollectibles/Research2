"""Read-only audit of published latency observations. Never runs a benchmark."""
from __future__ import annotations

from collections import Counter
import copy
from datetime import datetime, timezone
import gzip
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
PINNED = {
    "tuning": ("latency_selection.json", "fe6746bfb74ea2786398a0522b2e3631f4fa9a207906a443cb47b5f7715f64d3",
               "latency_selection_full.json.gz", "990cdeb9af4fcedfb7d54dad4fecb1f2bda3408440572caa1799ca4aa66f3d9e"),
    "heldout": ("latency.json", "ef9560c5982aafbead5f76aefd7b37352e6c3120c771c44ccb53575beda31571",
                "latency_full.json.gz", "c203f2d76cc027d06d04d2c669687c1c956e59c4183cac9690c3e0a19ad04e4a"),
}
MODEL = "latency_model_lambda0.5_K4"
GUARD = "miss_guard_K4"
FIXED = ("inline", "cache_delta", "miss_guard_K2", "miss_guard_K4", "recent_reprobe_W4K4")
MODELS = tuple(f"latency_model_lambda{f}_K{k}" for f, k in itertools.product(("0.5", "0.9"), (2, 4)))
HELDOUT_POLICIES = ("inline", "cache_delta", GUARD, "recent_reprobe_W4K4", MODEL)
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise RuntimeError(message)


def close(a, b, message):
    check(math.isclose(a, b, rel_tol=1e-11, abs_tol=1e-11), message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def load(split):
    summary_name, summary_sha, full_name, full_sha = PINNED[split]
    summary_bytes = (ROOT / "results" / summary_name).read_bytes()
    packed = (ROOT / "results" / full_name).read_bytes()
    check(sha(summary_bytes) == summary_sha and sha(packed) == full_sha, "Published file checksum")
    summary, full = json.loads(summary_bytes), json.loads(gzip.decompress(packed))
    check(summary["full_report"]["sha256"] == full_sha, "Summary archive checksum")
    check(summary["full_report"]["compressed_bytes"] == len(packed), "Compressed size")
    check(summary["full_report"]["uncompressed_bytes"] == len(gzip.decompress(packed)), "Uncompressed size")
    for field in ("experiment", "split", "source_commit", "design_commit", "selection",
                  "source_hashes", "planned_runs", "completed_runs", "failed_runs", "missing_runs"):
        check(summary[field] == full[field], "Summary/full disagreement: " + field)
    check(full["source_commit"] == "fb2e4aa3e1a76705bd8fc4aaa05d8c293588574a", "Frozen source commit")
    check(full["design_commit"] == "dac8aa0837ed78bc7e9688433e15bc2fef777a3f", "Frozen design commit")
    for name, expected in full["source_hashes"].items():
        path = (ROOT / name).resolve()
        check(path.is_relative_to(ROOT) and sha(path.read_bytes()) == expected, "Frozen file changed: " + name)
    return summary, full


def case_grid(split):
    if split == "tuning":
        sources = ((1024, 116101), (8192, 116129))
        workloads = (("healthy", 20), ("early_recovery", 14), ("late_recovery", 20))
        profiles = (("unshaped", 0.0, None), ("holdoff12_rate750k", .012, 750000))
    else:
        sources = ((1536, 127003), (12288, 127031))
        workloads = (("healthy", 24), ("short_single", 7), ("burst_recovery", 24), ("intermittent_recovery", 18))
        profiles = (("unshaped", 0.0, None), ("holdoff4_rate400k", .004, 400000),
                    ("holdoff16_rate1200k", .016, 1200000))
    return [{"split": split, "payload_chars": size, "seed": seed, "schedule": schedule, "handoffs": count,
             "profile": {"name": name, "reply_holdoff_seconds": delay, "framed_bytes_per_second": rate}}
            for (size, seed), (schedule, count), (name, delay, rate) in itertools.product(sources, workloads, profiles)]


def declared_order(split):
    rng = random.Random(116189 if split == "tuning" else 127081)
    policies = (*FIXED, *MODELS) if split == "tuning" else HELDOUT_POLICIES
    result = []
    for rep in range(3):
        cases = case_grid(split)
        rng.shuffle(cases)
        for case in cases:
            shuffled = list(policies)
            rng.shuffle(shuffled)
            result.extend((rep, case, policy) for policy in shuffled)
    return result


def source_artifacts(case, Artifact):
    rng = random.Random(case["seed"] ^ 0xA7C15)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    lengths = [case["payload_chars"] // 16 + int(i < case["payload_chars"] % 16) for i in range(16)]
    value = {f"part{i:02}": "".join(rng.choices(alphabet, k=length)) for i, length in enumerate(lengths)}
    current = Artifact.make(1, value)
    result = []
    for index in range(case["handoffs"]):
        if index and index % 5 == 0:
            value = copy.deepcopy(current.value)
            for part in rng.sample(range(16), 1):
                name = f"part{part:02}"
                old = value[name]
                value[name] = alphabet[(alphabet.index(old[0]) + 1) % len(alphabet)] + "".join(rng.choices(alphabet, k=len(old)-1))
            current = Artifact.make(current.ref.revision + 1, value)
        result.append(current)
    return result


def loss_flags(case):
    rng = random.Random(case["seed"] ^ 0x1A2B3C4D)
    flags = []
    for i in range(case["handoffs"]):
        draw, name = rng.random(), case["schedule"]
        lost = ((name == "early_recovery" and 1 <= i <= 5)
                or (name == "late_recovery" and 7 <= i <= 12)
                or (name == "short_single" and i == 2)
                or (name == "burst_recovery" and 2 <= i <= 9)
                or (name == "intermittent_recovery" and 1 <= i <= 10 and draw < .6))
        flags.append(lost)
    return flags


def audit_raw(full):
    # Only pure codec/decoder/policy APIs; no run_case, run_batch, socket or subprocess.
    from handoff_lab.adaptive_transport import Artifact, _delta, _decode_transfer, _message
    from handoff_lab.latency_experiment import Controller, PolicySpec, PacketPlan, Observation
    from handoff_lab.network_experiment import decode_handoff
    from handoff_lab.protocol import Envelope, canonical, digest
    rows = full["runs"]
    order = declared_order(full["split"])
    check(len(rows) == len(order) == full["planned_runs"], "Raw coverage")
    totals, by_policy = Counter(), {}
    for ordinal, (row, (rep, case, name)) in enumerate(zip(rows, order)):
        check(row["execution_ordinal"] == ordinal and row["repetition"] == rep
              and row["case"] == case and row["policy"] == name, "Independent execution order")
        check(row["completed"] and not row["missing"] and row["error"] is None, "Published all-correct claim")
        check(math.isfinite(row["completion_seconds"]) and row["completion_seconds"] > 0, "Valid wall time")
        policy = Controller(PolicySpec(**row["policy_spec"]))
        check(policy.spec.name == name, "Policy identity")
        artifacts, losses = source_artifacts(case, Artifact), loss_flags(case)
        check(losses == row["receiver_executed_loss_events"], "Independent loss schedule")
        check([a.ref.sha256 for a in artifacts] == row["receiver_output_digests"], "Independent source/output digests")
        check(len(row["handoff_records"]) == len(artifacts), "Complete handoff trace")
        streams = {"up": hashlib.sha256(), "down": hashlib.sha256()}
        counters = Counter()
        def frame(wire, direction):
            encoded = wire.encode("utf-8")
            raw = struct.pack("!I", len(encoded)) + encoded
            streams[direction].update(raw)
            counters[direction + "_bytes"] += len(raw)
            counters[direction + "_payload"] += len(encoded)
            counters[direction + "_frames"] += 1
        caps = canonical({"kind": "capabilities", "v": 1, "topology": "delta_sync",
                          "deployment": "direct", "cache_status": False})
        frame(caps, "up")
        known = cached = None
        sums = Counter()
        for index, (event, current) in enumerate(zip(row["handoff_records"], artifacts)):
            check(event["index"] == index, "Event order")
            inline = _message(current, index, True)
            ack = canonical({"kind": "ack", "ref": current.ref.to_dict(), "retained": True})
            absent_get = canonical({"kind": "get", "ref": current.ref.to_dict(), "have": None})
            snapshot = canonical({"kind": "snapshot", "ref": current.ref.to_dict(), "value": current.value})
            cache_wire = cache_kind = None
            if name != "inline" and known is not None:
                if known.ref == current.ref:
                    cache_kind, cache_wire = "reference", _message(current, index, False)
                else:
                    delta = _delta(known, current)
                    if delta is not None:
                        cache_kind = "delta"
                        cache_wire = Envelope("sweep", "update", "producer", "consumer",
                            {"event": index, "transfer": json.loads(delta)}).encode()
            quote_recovery = policy.needs_recovery_sizes and cache_kind is not None
            plan = PacketPlan(index, len(inline.encode()), cache_kind,
                len(cache_wire.encode()) if cache_wire is not None else None, len(ack.encode()),
                len(absent_get.encode()) if quote_recovery else None,
                len(snapshot.encode()) if quote_recovery else None)
            check(plan.to_dict() == event["packet_plan"], "Independent canonical packet lengths")
            # The hidden loss is applied only AFTER the decision is replayed.
            decision = policy.choose(plan)
            check(decision.to_dict() == event["decision"], "Past-only policy replay")
            wire = inline if decision.action == "inline" else cache_wire
            check(wire is not None and sha(wire.encode()) == event["data_payload_sha256"], "Actual data payload digest")
            before = counters["up_bytes"] + counters["down_bytes"]
            frame(wire, "up")
            if losses[index]:
                cached = None
            target, resolved, action = decode_handoff(wire, index, cached)
            miss = resolved is None
            if miss:
                check(cached is None, "This declared loss study clears the entire cache")
                frame(absent_get, "down")
                frame(snapshot, "up")
                resolved = _decode_transfer(snapshot, cached)
                counters["misses"] += 1
            check(resolved.ref == current.ref == target and resolved.value == current.value
                  and digest(resolved.value) == target.sha256, "Actual wire reconstruction")
            frame(ack, "down")
            raw_observation = event["observation"]
            check(raw_observation["action"] == action == decision.action, "Observed action")
            check(raw_observation["framed_bytes"] == counters["up_bytes"] + counters["down_bytes"] - before,
                  "Observed framed bytes")
            check(raw_observation["reply_count"] == 1 + miss and raw_observation["get_observed"] == miss,
                  "Observed real GET/ACK feedback")
            check(event["data_payload_bytes"] == len(wire.encode()) and event["ack_payload_bytes"] == len(ack.encode()),
                  "Data/ACK lengths")
            check(event["get_payload_bytes"] == (len(absent_get.encode()) if miss else 0)
                  and event["recovery_payload_bytes"] == (len(snapshot.encode()) if miss else 0), "Recovery lengths")
            observation = Observation(**raw_observation)
            policy.observe(observation)
            counters["forced"] += int(decision.forced_probe)
            counters["handoffs"] += 1
            for field in ("codec_quote_seconds", "codec_construction_recovery_seconds",
                          "policy_selection_seconds", "policy_observation_seconds"):
                check(math.isfinite(event[field]) and event[field] >= 0, "Finite timed computation")
                sums[field] += event[field]
            sums["service_seconds"] += observation.service_seconds
            known, cached = current, resolved
        sender, receiver = row["sender_transport"], row["receiver_transport"]
        for direction, sent, received in (("up", sender, receiver), ("down", receiver, sender)):
            check(streams[direction].hexdigest() == sent["sent_stream_sha256"] == received["received_stream_sha256"],
                  "Rebuilt complete framed-stream SHA256")
            for suffix, field in (("bytes", "stream_bytes"), ("payload", "payload_bytes"), ("frames", "frames")):
                check(counters[direction + "_" + suffix] == sent["sent_" + field] == received["received_" + field],
                      "Endpoint byte/frame reconciliation")
            rate = case["profile"]["framed_bytes_per_second"]
            requested = counters[direction + "_bytes"] / rate if rate else 0.0
            close(sent["requested_pacing_seconds"], requested, "Per-direction pacing budget")
            check(sent["observed_pacing_wait_seconds"] + 1e-6 >= requested, "Real pacing waits")
        frames = counters["up_frames"] + counters["down_frames"]
        byte_count = counters["up_bytes"] + counters["down_bytes"]
        check(row["framed_stream_bytes"] == byte_count and row["frames"] == frames, "Session byte/frame totals")
        check(byte_count == row["payload_bytes"] + row["application_framing_bytes"]
              and row["application_framing_bytes"] == 4 * frames, "Physical frame prefixes")
        check(row["recovery_requests"] == row["recovery_responses"] == counters["misses"], "All recoveries charged")
        check(row["forced_probes"] == counters["forced"] and row["acknowledgments"] == row["data_frames"] == case["handoffs"],
              "Completed actions and probes")
        holdoff = (case["handoffs"] + counters["misses"]) * case["profile"]["reply_holdoff_seconds"]
        close(receiver["requested_holdoff_seconds"], holdoff, "All GET/ACK holdoffs charged")
        check(sender["requested_holdoff_seconds"] == 0 and receiver["observed_holdoff_wait_seconds"] + 1e-6 >= holdoff,
              "Actual directional holdoff")
        pacing = sender["requested_pacing_seconds"] + receiver["requested_pacing_seconds"]
        check(row["completion_seconds"] + 1e-6 >= holdoff + pacing, "Imposed waits inside completion time")
        for field in ("codec_quote_seconds", "codec_construction_recovery_seconds",
                      "policy_selection_seconds", "policy_observation_seconds"):
            close(row[field], sums[field], "Computation timing sum")
        check(row["completion_seconds"] + 1e-8 >= sums["service_seconds"] + sums["codec_quote_seconds"]
              + sums["policy_selection_seconds"] + sums["policy_observation_seconds"], "Measured work inside timer")
        counters["stream_bytes"], counters["frames"], counters["sessions"] = byte_count, frames, 1
        totals.update({field: counters[field] for field in ("sessions", "handoffs", "stream_bytes", "frames", "misses", "forced")})
        entry = by_policy.setdefault(name, Counter())
        entry.update({field: counters[field] for field in ("sessions", "handoffs", "stream_bytes", "frames", "misses", "forced")})
    check(full["completed_runs"] == len(rows) and full["failed_runs"] == full["missing_runs"] == 0, "Outcome counts")
    return {"totals": dict(totals), "by_policy": {name: dict(value) for name, value in by_policy.items()}}


def observations(full):
    result = {(key(row["case"]), row["policy"], row["repetition"]): row["completion_seconds"] for row in full["runs"]}
    check(len(result) == len(full["runs"]), "No duplicate cells")
    return result


def paired(times, case, numerator, denominator):
    return [times[key(case), numerator, rep] / times[key(case), denominator, rep] for rep in range(3)]


def aggregate(times, split, numerator, denominator):
    return math.exp(statistics.mean(math.log(statistics.median(paired(times, case, numerator, denominator)))
                                    for case in case_grid(split)))


def recompute_selection(full):
    times = observations(full)
    scores = {name: aggregate(times, "tuning", name, "cache_delta") for name in (*FIXED, *MODELS)}
    for name, value in scores.items():
        close(value, full["tuning"]["scores"][name], "Independent tuning score")
    guard = min((2, 4), key=lambda k: (scores[f"miss_guard_K{k}"], k))
    factor, interval = min(itertools.product((.5, .9), (2, 4)),
                          key=lambda fk: (scores[f"latency_model_lambda{fk[0]}_K{fk[1]}"], fk[1], -fk[0]))
    simple = ("cache_delta", "inline", f"miss_guard_K{guard}")
    baseline = min(enumerate(simple), key=lambda item: (scores[item[1]], item[0]))[1]
    selected = {"miss_guard": {"family": "miss_guard", "interval": guard, "forgetting": None},
                "model": {"family": "latency_model", "interval": interval, "forgetting": factor},
                "simple_baseline": baseline}
    check(selected == full["selection"] == full["tuning"]["selection"], "Independent frozen selection")
    return {"selection": selected, "scores": scores,
            "direct_selected_model_over_guard": aggregate(times, "tuning", MODEL, GUARD)}


def recompute_heldout(summary, full):
    times = observations(full)
    ratios = {name: aggregate(times, "heldout", MODEL, name) for name in HELDOUT_POLICIES if name != MODEL}
    saved = full["heldout_analysis"]
    for name, value in ratios.items():
        close(value, saved["model_comparisons"][name]["equal_case_geometric_mean_paired_ratio"], "Direct paired aggregate")
        for case in case_grid("heldout"):
            found = next(item for item in saved["model_comparisons"][name]["case_comparisons"] if item["case"] == case)
            expected = paired(times, case, MODEL, name)
            for rep in range(3):
                close(found["pairs"][rep]["ratio"], expected[rep], "Every saved paired ratio")
            close(found["median_paired_ratio"], statistics.median(expected), "Every saved paired median")
    healthy, hindsight, cases_out = [], [], []
    winners = Counter()
    for case in case_grid("heldout"):
        medians = {name: statistics.median(times[key(case), name, rep] for rep in range(3)) for name in HELDOUT_POLICIES}
        winner = min(HELDOUT_POLICIES, key=lambda name: medians[name])
        winners[winner] += 1
        best_simple = min(("inline", "cache_delta", GUARD), key=lambda name: medians[name])
        hindsight.append(medians[MODEL] / medians[best_simple])
        if case["schedule"] == "healthy":
            excess = statistics.median(times[key(case), MODEL, rep] - times[key(case), "cache_delta", rep] for rep in range(3))
            allowance = max(.0005, .05 * medians["cache_delta"])
            healthy.append({"case": case, "median_paired_excess_seconds": excess,
                            "allowance_seconds": allowance, "pass": excess <= allowance})
        summary_case = next(item for item in summary["case_summaries"] if item["case"] == case)
        for name, value in medians.items():
            close(summary_case["policies"][name]["median_completion_seconds"], value, "Stored median recomputed")
        cases_out.append({"case": case, "median_seconds": medians, "winner": winner,
                          "direct_model_guard_paired_ratios": paired(times, case, MODEL, GUARD),
                          "hindsight_ratio_of_medians": hindsight[-1]})
    gates = {
        "all_planned_runs_correct": len(full["runs"]) == 360,  # Every wire/output was independently checked first.
        "five_percent_vs_validation_selected_simple": ratios[GUARD] <= .95,
        "parity_or_better_vs_each_simple": all(ratios[name] <= 1 for name in ("inline", "cache_delta", GUARD)),
        "healthy_overhead_bounded": all(item["pass"] for item in healthy),
        "worst_hindsight_ratio_at_most_1p25": max(hindsight) <= 1.25,
    }
    check(gates == saved["gates"] == summary["heldout_analysis"]["gates"], "All five gates independently recomputed")
    close(max(hindsight), saved["worst_hindsight_ratio_of_medians"], "Hindsight robustness bound")
    close(ratios[GUARD], saved["primary_equal_case_geometric_mean_paired_ratio"], "Direct primary guard contrast")
    check(all(gates.values()) == saved["all_engineering_gates_met"], "Overall gate result")
    return {"direct_paired_aggregates": ratios, "gates": gates, "all_gates_met": all(gates.values()),
            "worst_hindsight_ratio_of_medians": max(hindsight), "healthy": healthy,
            "median_winner_counts": dict(winners), "cases": cases_out}


def main():
    output = ROOT / "audit/latency_audit.json"
    report = {"audit_only": True, "benchmark_invocations": 0, "started_utc": datetime.now(timezone.utc).isoformat(),
              "script_sha256": sha(Path(__file__).read_bytes()), "input_hashes": PINNED,
              "github_run_id": os.environ.get("GITHUB_RUN_ID"), "github_sha": os.environ.get("GITHUB_SHA"),
              "python": sys.version, "passed": False}
    try:
        check(platform.python_version() == "3.12.14", "Audit uses the same Python minor/patch")
        tuning_summary, tuning = load("tuning")
        heldout_summary, heldout = load("heldout")
        check(tuning["source_hashes"] == heldout["source_hashes"], "Frozen code unchanged between stages")
        check(heldout["selection_commit"] == "eb6f9966260aec784f8eaa9ab98e3d0761f614f3", "Pre-outcome selection publication")
        sys.path.insert(0, str(ROOT / "src"))
        report["tuning_raw_audit"] = audit_raw(tuning)
        report["selection"] = recompute_selection(tuning)
        check(heldout["selection"] == report["selection"]["selection"], "Held-out uses independently recomputed selection")
        report["heldout_raw_audit"] = audit_raw(heldout)
        report["heldout"] = recompute_heldout(heldout_summary, heldout)
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["checks"] = CHECKS
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n")
        print(json.dumps({key: report.get(key) for key in ("passed", "checks", "error", "benchmark_invocations",
                                                         "tuning_raw_audit", "heldout_raw_audit")}), flush=True)
        if report["passed"]:
            print(json.dumps({"selection": report["selection"], "heldout_comparisons": report["heldout"]["direct_paired_aggregates"],
                              "gates": report["heldout"]["gates"],
                              "worst_hindsight_ratio": report["heldout"]["worst_hindsight_ratio_of_medians"]}), flush=True)


if __name__ == "__main__":
    main()
