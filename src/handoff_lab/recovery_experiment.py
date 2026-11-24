"""Frozen v1 study of cache loss, bounded re-probing, and paid status feedback.

Run tuning first; commit its code/selection before invoking evaluate. No model
or network calls occur. The original transport experiment remains unchanged.
"""
from __future__ import annotations

import argparse
from collections import deque
import copy
from dataclasses import dataclass
import gzip
import hashlib
import itertools
import json
from pathlib import Path
import random
import statistics
import sys
import time

from .adaptive_transport import (Artifact, Ledger, OnlinePolicy, Transmission, View,
                                  _message, account, cheapest)
from .protocol import ProtocolError, Ref, canonical, nbytes, parse_json

DESIGN_COMMIT = "94e5bfd0164f60cc7046d6719d8df74d218fa02d"
CANDIDATES = tuple(itertools.product((4, 8), (2, 4, 8)))
FIXED = ("inline", "cache_delta", "inline_once_cache")
POLICIES = (*FIXED, "original_online", "recent_reprobe", "status_feedback")
ROOT = Path(__file__).resolve().parents[2]
STATUS_PROTOCOL = {
    "encoding": "protocol.canonical: UTF-8 JSON, sorted keys, compact separators",
    "reference_schema": {"id": "artifact identifier", "rev": "positive integer", "sha256": "64 lowercase hexadecimal characters"},
    "request_fields": {"kind": "cache_status", "task": "sweep", "want": "reference object"},
    "reply_fields": {"kind": "cache_status_reply", "have": "reference object or null"},
    "capability_advertisement": {"cache_status": True},
    "information_assumption": "No eviction between the charged status reply and data delivery",
}


@dataclass(frozen=True)
class RecoverySpec:
    payload_chars: int
    uses: int
    update_every: int
    changed_fields: int
    topology: str
    seed: int
    split: str
    schedule: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class RecoveryWorkload:
    spec: RecoverySpec
    artifacts: tuple[Artifact, ...]
    losses: tuple[bool, ...]
    phases: tuple[tuple[str, str], ...]


def phase_at(split: str, schedule: str, index: int) -> tuple[float, str, str]:
    """Absolute indices only: no session length is available to this function."""
    if schedule == "healthy":
        return 0.0, "healthy", "healthy"
    if schedule == "permanent_loss":
        return (0.0, "healthy", "initial") if index == 0 else (1.0, "loss", "loss")
    if schedule == "loss_recovery":
        boundary = 8 if split == "tuning" else 10
        if index == 0:
            return 0.0, "healthy", "initial"
        return (1.0, "loss", "loss") if index < boundary else (0.0, "recovery", "recovery")
    if schedule == "healthy_loss_recovery":
        start, end = (4, 12) if split == "tuning" else (5, 15)
        if index < start:
            return 0.0, "healthy", "initial"
        return (1.0, "loss", "loss") if index < end else (0.0, "recovery", "recovery")
    if schedule == "intermittent_recovery" and split == "heldout":
        if index == 0:
            return 0.0, "healthy", "initial"
        return (0.75, "loss", "intermittent") if index < 18 else (0.0, "recovery", "recovery")
    if split == "stress":
        if schedule == "early_single_loss":
            if index == 0:
                return 0.0, "healthy", "initial"
            return (1.0, "loss", "loss") if index == 1 else (0.0, "recovery", "recovery")
        if schedule == "burst_cycles":
            if index < 8:
                return 0.0, "healthy", "initial"
            if index < 16:
                return 1.0, "loss", "loss1"
            if index < 32:
                return 0.0, "recovery", "recovery1"
            return (1.0, "loss", "loss2") if index < 40 else (0.0, "recovery", "recovery2")
        if schedule == "end_recovery":
            if index == 0:
                return 0.0, "healthy", "initial"
            return (1.0, "loss", "loss") if index < 22 else (0.0, "recovery", "recovery")
        if schedule == "alternate_three":
            block = index // 3
            if block == 0:
                return 0.0, "healthy", "initial"
            return (1.0, "loss", f"loss{block}") if block % 2 else (0.0, "recovery", f"recovery{block}")
    raise ValueError("Unknown split/schedule")


def generate(spec: RecoverySpec) -> RecoveryWorkload:
    if (spec.payload_chars < 16 or spec.uses < 1 or spec.update_every < 0 or
            not 0 <= spec.changed_fields <= 16 or spec.topology not in {"revision_cache", "delta_sync"}):
        raise ValueError("Invalid recovery workload")
    if spec.update_every and not spec.changed_fields:
        raise ValueError("Updates require changed fields")
    artifact_rng = random.Random(spec.seed ^ 0xA7C15)
    loss_rng = random.Random(spec.seed ^ 0x5EED5EED)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    lengths = [spec.payload_chars // 16 + int(i < spec.payload_chars % 16) for i in range(16)]
    value = {f"part{i:02}": "".join(artifact_rng.choices(alphabet, k=length))
             for i, length in enumerate(lengths)}
    current = Artifact.make(1, value)
    artifacts, losses, phases = [], [], []
    for index in range(spec.uses):
        if index and spec.update_every and index % spec.update_every == 0:
            value = copy.deepcopy(current.value)
            for part in artifact_rng.sample(range(16), spec.changed_fields):
                key = f"part{part:02}"
                old = value[key]
                value[key] = alphabet[(alphabet.index(old[0]) + 1) % len(alphabet)] + "".join(
                    artifact_rng.choices(alphabet, k=len(old) - 1))
            current = Artifact.make(current.ref.revision + 1, value)
        probability, phase, segment = phase_at(spec.split, spec.schedule, index)
        draw = loss_rng.random()  # Separate stream, one draw per absolute event.
        losses.append(bool(index and draw < probability))
        phases.append((phase, segment))
        artifacts.append(current)
    return RecoveryWorkload(spec, tuple(artifacts), tuple(losses), tuple(phases))


def specs(split: str) -> list[RecoverySpec]:
    if split == "tuning":
        sizes, lengths, updates, seeds = (384, 3072), (12, 36), ((0, 0), (3, 1), (3, 8)), (57103, 57119)
        schedules = ("healthy", "permanent_loss", "loss_recovery", "healthy_loss_recovery")
    elif split == "heldout":
        sizes, lengths, updates, seeds = (192, 1536, 12288), (1, 3, 15, 45), ((0, 0), (2, 1), (5, 8)), (68311, 68329)
        schedules = ("healthy", "permanent_loss", "loss_recovery", "healthy_loss_recovery", "intermittent_recovery")
    elif split == "stress":
        return [RecoverySpec(size, length, 4, 1, topology, 79433, split, schedule)
                for size, topology, (schedule, length) in itertools.product(
                    (256, 4096), ("revision_cache", "delta_sync"),
                    (("early_single_loss", 32), ("burst_cycles", 64), ("end_recovery", 24), ("alternate_three", 48)))]
    else:
        raise ValueError("Unknown split")
    return [RecoverySpec(size, length, update[0], update[1], topology, seed, split, schedule)
            for size, length, update, topology, seed, schedule in itertools.product(
                sizes, lengths, updates, ("revision_cache", "delta_sync"), seeds, schedules)]


def inline_tx(current: Artifact, index: int) -> Transmission:
    return Transmission("inline", _message(current, index, True), None, None, True)


def opportunity(current: Artifact, index: int, view: View) -> tuple[bool, Transmission]:
    zero_loss, _, normal = cheapest(current, index, view, True, 0.0)
    inline = account(inline_tx(current, index), current, view, view.known_cache, validate=False).total_bytes
    return zero_loss.kind in {"reference", "delta"} and normal < inline, zero_loss


class RecentReprobe:
    def __init__(self, window: int, interval: int):
        if type(window) is not int or type(interval) is not int or window < 1 or interval < 1:
            raise ValueError("Positive window and interval required")
        self.window = deque(maxlen=window)
        self.interval = interval
        self.suppressed = 0

    def choose(self, current: Artifact, index: int, view: View) -> tuple[Transmission, bool, bool]:
        eligible, probe = opportunity(current, index, view)
        probability = sum(self.window) / len(self.window) if self.window else 0.0
        chosen, _, _ = cheapest(current, index, view, True, probability)
        forced = False
        if chosen.kind in {"reference", "delta"}:
            self.suppressed = 0
        elif eligible:
            self.suppressed += 1
            if self.suppressed >= self.interval:
                chosen, forced = probe, True
                self.suppressed = 0
        return chosen, forced, eligible

    def observe(self, current: Artifact, view: View, tx: Transmission, ledger: Ledger) -> None:
        expected = account(tx, current, view, view.known_cache, validate=False)
        if tx.kind in {"reference", "delta"} and expected.cache_misses == 0:
            self.window.append(int(ledger.cache_misses > 0))


def decode_status_reply(reply: str, view: View) -> View:
    value = parse_json(reply)
    if type(value) is not dict or set(value) != {"kind", "have"} or value["kind"] != "cache_status_reply":
        raise ProtocolError("Invalid cache-status reply")
    if value["have"] is None:
        return View(view.topology, view.deployment, None, view.published)
    ref = Ref.from_dict(value["have"])
    if view.known_cache is None or ref != view.known_cache.ref:
        raise ProtocolError("Status reply contains an unsupported retained revision")
    return View(view.topology, view.deployment, view.known_cache, view.published)


def status_exchange(current: Artifact, view: View, actual_cache: Artifact | None) -> tuple[View, int, int]:
    request = canonical({"kind": "cache_status", "task": "sweep", "want": current.ref.to_dict()})
    decoded_request = parse_json(request)
    if Ref.from_dict(decoded_request["want"]) != current.ref:
        raise ProtocolError("Status query targets wrong revision")
    reply = canonical({"kind": "cache_status_reply", "have": actual_cache.ref.to_dict() if actual_cache else None})
    # The controller receives only the decoded serialized reply. The cache object
    # is used solely by the simulated responding endpoint.
    return decode_status_reply(reply, view), nbytes(request), nbytes(reply)


def run(workload: RecoveryWorkload, policy: str, *, window: int = 4, interval: int = 4,
        trace: bool = False, measure_time: bool = False) -> dict:
    if policy not in POLICIES:
        raise ValueError("Unknown recovery policy")
    recent = RecentReprobe(window, interval)
    original = OnlinePolicy(0.0)
    cached = known = None
    total = Ledger()
    status_request_bytes = status_response_bytes = status_queries = forced_attempts = 0
    decision_ns = 0
    phases = {}
    recovery_segments = {}
    history = []
    actions = {"inline": 0, "reference": 0, "delta": 0}
    setup = nbytes(canonical({"kind": "capabilities", "v": 1, "topology": workload.spec.topology,
                             "deployment": "direct", "cache_status": True}))
    for index, current in enumerate(workload.artifacts):
        if workload.losses[index]:
            cached = None
        view = View(workload.spec.topology, "direct", known, None)
        delivery_view = view
        request_bytes = response_bytes = 0
        queried = forced = False
        eligible = None
        started = time.perf_counter_ns() if measure_time else 0
        if policy == "inline" or (policy == "inline_once_cache" and index == 0):
            tx = inline_tx(current, index)
        elif policy in {"cache_delta", "inline_once_cache"}:
            tx, _, _ = cheapest(current, index, view, True)
        elif policy == "original_online":
            tx = original.choose(current, index, view)
        elif policy == "recent_reprobe":
            tx, forced, eligible = recent.choose(current, index, view)
        else:
            eligible, _ = opportunity(current, index, view)
            if eligible:
                delivery_view, request_bytes, response_bytes = status_exchange(current, view, cached)
                queried = True
                tx, _, _ = cheapest(current, index, delivery_view, True)
            else:
                tx = inline_tx(current, index)
        if measure_time:
            decision_ns += time.perf_counter_ns() - started
        ledger = account(tx, current, delivery_view, cached, validate=True)
        started = time.perf_counter_ns() if measure_time else 0
        if policy == "recent_reprobe":
            recent.observe(current, view, tx, ledger)
        elif policy == "original_online":
            original.observe(current, index, view, tx, ledger)
        if measure_time:
            decision_ns += time.perf_counter_ns() - started
        if eligible is None:
            # Evaluator-only metric; this quote is outside decision timing.
            eligible, _ = opportunity(current, index, view)
        cached = known = current
        actions[tx.kind] += 1
        status_request_bytes += request_bytes
        status_response_bytes += response_bytes
        status_queries += int(queried)
        forced_attempts += int(forced)
        for key, value in ledger.__dict__.items():
            setattr(total, key, getattr(total, key) + value)
        phase, segment = workload.phases[index]
        phase_row = phases.setdefault(phase, {"handoffs": 0, "bytes": 0, "cache_misses": 0,
                                              "forced_reprobes": 0, "status_queries": 0})
        phase_row["handoffs"] += 1
        phase_row["bytes"] += ledger.total_bytes + request_bytes + response_bytes
        phase_row["cache_misses"] += ledger.cache_misses
        phase_row["forced_reprobes"] += int(forced)
        phase_row["status_queries"] += int(queried)
        if phase == "recovery":
            recovery = recovery_segments.setdefault(segment, {"start_index": index,
                "eligible_opportunities": 0, "eligible_before_first_success": None,
                "first_success_index": None, "censored": True})
            if eligible:
                if tx.kind in {"reference", "delta"} and not ledger.cache_misses and recovery["censored"]:
                    recovery["first_success_index"] = index
                    recovery["eligible_before_first_success"] = recovery["eligible_opportunities"]
                    recovery["censored"] = False
                recovery["eligible_opportunities"] += 1
        if trace:
            history.append({"index": index, "phase": phase, "segment": segment,
                "revision": current.ref.revision, "action": tx.kind, "forced_reprobe": forced,
                "status_query": queried, "eligible": eligible, "silent_loss": workload.losses[index],
                **ledger.to_dict(), "status_request_bytes": request_bytes,
                "status_response_bytes": response_bytes,
                "total_bytes": ledger.total_bytes + request_bytes + response_bytes})
    return {"policy": policy, **total.to_dict(), "capability_setup_bytes": setup,
            "status_request_bytes": status_request_bytes, "status_response_bytes": status_response_bytes,
            "total_bytes": total.total_bytes + setup + status_request_bytes + status_response_bytes,
            "status_queries": status_queries, "forced_reprobes": forced_attempts,
            "logical_round_trips": workload.spec.uses + total.cache_misses + status_queries,
            "actions": actions, "phases": phases, "recovery_segments": recovery_segments,
            "all_reconstructions_exact": True,
            "decision_elapsed_nanoseconds": decision_ns if measure_time else None,
            **({"trace": history} if trace else {})}


def fingerprints() -> dict:
    files = ("src/handoff_lab/recovery_experiment.py", "src/handoff_lab/adaptive_transport.py",
             "src/handoff_lab/protocol.py")
    return {"design_commit": DESIGN_COMMIT,
            "design_sha256": hashlib.sha256((ROOT / "docs/recovery_design.md").read_bytes()).hexdigest(),
            "implementation_sha256": {file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in files}}


def tune(*, progress=None) -> dict:
    scores = {(w, k): {"total_bytes": 0, "healthy_regressions": 0} for w, k in CANDIDATES}
    fixed_scores = {topology: {name: 0 for name in FIXED} for topology in ("revision_cache", "delta_sync")}
    rows = []
    workloads = specs("tuning")
    for index, spec in enumerate(workloads):
        workload = generate(spec)
        fixed = {name: run(workload, name) for name in FIXED}
        candidate_results = {}
        for w, k in CANDIDATES:
            result = run(workload, "recent_reprobe", window=w, interval=k)
            scores[(w, k)]["total_bytes"] += result["total_bytes"]
            if not any(workload.losses) and result["total_bytes"] > fixed["cache_delta"]["total_bytes"]:
                scores[(w, k)]["healthy_regressions"] += 1
            candidate_results[f"w{w}_k{k}"] = {key: result[key] for key in
                ("total_bytes", "cache_misses", "forced_reprobes", "logical_round_trips", "all_reconstructions_exact")}
        for name in FIXED:
            fixed_scores[spec.topology][name] += fixed[name]["total_bytes"]
        rows.append({"spec": spec.to_dict(), "candidates": candidate_results,
                     "fixed_total_bytes": {name: result["total_bytes"] for name, result in fixed.items()}})
        if progress and index % 24 == 0:
            progress("tuning", index, len(workloads))
    admissible = [pair for pair, result in scores.items() if result["healthy_regressions"] == 0]
    selected = min(admissible, key=lambda pair: (scores[pair]["total_bytes"], pair[0], pair[1])) if admissible else None
    return {"schema_version": 1, "experiment": "cache_recovery_v1_selection", **fingerprints(),
            "status_feedback_protocol": STATUS_PROTOCOL,
            "heldout_run": False, "model_calls_performed": 0, "tuning_workloads": len(workloads),
            "selection_success": selected is not None,
            "selected_parameters": {"window": selected[0], "interval": selected[1]} if selected else None,
            "selection_rule": "Minimum tuning aggregate bytes subject to zero healthy regressions; tie by W then K",
            "candidate_scores": {f"w{w}_k{k}": result for (w, k), result in scores.items()},
            "fixed_scores": fixed_scores,
            "selected_fixed_by_topology": {topology: min(values, key=lambda name: (values[name], name))
                                           for topology, values in fixed_scores.items()},
            "tuning_rows": rows}


def validate_selection(selection: dict) -> None:
    if (selection.get("experiment") != "cache_recovery_v1_selection" or
            not selection.get("selection_success") or selection.get("heldout_run") is not False):
        raise ValueError("A successful frozen tuning selection is required")
    for key, value in fingerprints().items():
        if selection.get(key) != value:
            raise ValueError(f"Frozen selection mismatch: {key}; do not silently change the design or implementation")
    parameters = selection.get("selected_parameters", {})
    if (parameters.get("window"), parameters.get("interval")) not in CANDIDATES:
        raise ValueError("Selected parameters are outside the frozen candidate set")
    comparators = selection.get("selected_fixed_by_topology", {})
    if set(comparators) != {"revision_cache", "delta_sync"} or any(name not in FIXED for name in comparators.values()):
        raise ValueError("Invalid frozen fixed comparators")


def summarize(rows: list[dict], selected_fixed: dict) -> dict:
    totals = {name: sum(row["policies"][name]["total_bytes"] for row in rows) for name in POLICIES}
    selected_total = sum(row["policies"][selected_fixed[row["spec"]["topology"]]]["total_bytes"] for row in rows)
    oracle = sum(min(row["policies"][name]["total_bytes"] for name in FIXED) for row in rows)
    ratios = [row["policies"]["recent_reprobe"]["total_bytes"] /
              min(row["policies"][name]["total_bytes"] for name in FIXED) for row in rows]
    recovery = {}
    for name in POLICIES:
        segments = [segment for row in rows for segment in row["policies"][name]["recovery_segments"].values()]
        successful_lags = [segment["eligible_before_first_success"] for segment in segments if not segment["censored"]]
        recovery[name] = {"segments": len(segments), "censored_segments": sum(s["censored"] for s in segments),
                          "segments_with_opportunities": sum(s["eligible_opportunities"] > 0 for s in segments),
                          "censored_segments_with_opportunities": sum(s["censored"] and s["eligible_opportunities"] > 0 for s in segments),
                          "mean_eligible_before_success": statistics.mean(successful_lags) if successful_lags else None}
    phase_totals = {}
    for phase in ("healthy", "loss", "recovery"):
        phase_totals[phase] = {name: sum(row["policies"][name]["phases"].get(phase, {}).get("bytes", 0)
                                        for row in rows) for name in POLICIES}
    timings = {}
    for name in POLICIES:
        values = [row["policies"][name]["decision_elapsed_nanoseconds"] for row in rows]
        timings[name] = sum(values) / sum(row["spec"]["uses"] for row in rows) / 1000 if all(v is not None for v in values) else None
    return {"workloads": len(rows), "policy_total_bytes": totals,
            "tuning_selected_fixed_total_bytes": selected_total,
            "hindsight_best_fixed_per_workload_bytes": oracle,
            "recent_reduction_vs_original_fraction": 1 - totals["recent_reprobe"] / totals["original_online"],
            "recent_reduction_vs_selected_fixed_fraction": 1 - totals["recent_reprobe"] / selected_total,
            "recent_to_hindsight_best_fixed_median_ratio": statistics.median(ratios),
            "recent_to_hindsight_best_fixed_worst_ratio": max(ratios),
            "recent_losses_vs_hindsight_fixed_workloads": sum(ratio > 1 for ratio in ratios),
            "status_reduction_vs_cache_delta_fraction": 1 - totals["status_feedback"] / totals["cache_delta"],
            "forced_reprobes": sum(row["policies"]["recent_reprobe"]["forced_reprobes"] for row in rows),
            "policy_logical_round_trips": {name: sum(row["policies"][name]["logical_round_trips"] for row in rows)
                                           for name in POLICIES},
            "policy_mean_decision_elapsed_microseconds_per_handoff": timings,
            "phase_total_bytes_excluding_initial_setup": phase_totals, "recovery_use": recovery}


def evaluate(selection: dict, *, measure_time: bool = False, progress=None) -> dict:
    validate_selection(selection)
    parameters = selection["selected_parameters"]
    result_rows = {}
    for split in ("heldout", "stress"):
        rows = []
        workloads = specs(split)
        for index, spec in enumerate(workloads):
            workload = generate(spec)
            rows.append({"spec": spec.to_dict(), "actual_loss_count": sum(workload.losses),
                         "policies": {name: run(workload, name, **parameters, measure_time=measure_time)
                                      for name in POLICIES}})
            if progress and index % 48 == 0:
                progress(split, index, len(workloads))
        result_rows[split] = rows
    selected = selection["selected_fixed_by_topology"]
    heldout, stress = result_rows["heldout"], result_rows["stress"]
    healthy = [row for row in heldout if row["spec"]["schedule"] == "healthy"]
    no_loss = [row for row in heldout if not row["actual_loss_count"]]
    return {"schema_version": 1, "experiment": "cache_recovery_v1_evaluation", **fingerprints(),
            "status_feedback_protocol": STATUS_PROTOCOL,
            "selection_parameters": parameters, "selected_fixed_by_topology": selected,
            "model_calls_performed": 0, "tokenizer_used": None,
            "heldout_summary": summarize(heldout, selected), "stress_summary": summarize(stress, selected),
            "healthy_controls": {"workloads": len(healthy),
                "recent_bytes_equal_cache_delta_every_workload": all(row["policies"]["recent_reprobe"]["total_bytes"] ==
                    row["policies"]["cache_delta"]["total_bytes"] for row in healthy),
                "all_no_loss_workloads": len(no_loss),
                "recent_bytes_equal_cache_delta_every_no_loss_workload": all(row["policies"]["recent_reprobe"]["total_bytes"] ==
                    row["policies"]["cache_delta"]["total_bytes"] for row in no_loss),
                "summary": summarize(healthy, selected)},
            "by_topology": {topology: summarize([row for row in heldout if row["spec"]["topology"] == topology], selected)
                            for topology in ("revision_cache", "delta_sync")},
            "by_schedule": {schedule: summarize([row for row in heldout if row["spec"]["schedule"] == schedule], selected)
                            for schedule in sorted({row["spec"]["schedule"] for row in heldout})},
            "assumptions": [
                "Direct cache only; every policy has the same retained content, delta support and charged acknowledgments.",
                "Loss phases use absolute indices; policy code does not receive phase, future length or current hidden loss.",
                "Recent-history parameters and fixed comparator choices were frozen on new tuning data before held-out outcomes.",
                "Re-probes are ordinary charged data deliveries; failures include their full recovery traffic.",
                "Status feedback is a separately priced information intervention, with serialized request/reply and an additional exchange.",
                "Status replies are accurate and no eviction occurs between status and data delivery.",
                "Application payload bytes and logical exchanges exclude real network framing, I/O, compression and latency.",
                "No token, model accuracy, inference speed, real-agent trace or scientific-novelty claim is established.",
                "Phase bytes exclude one-time capability setup; workload totals include it.",
                "Recovery segments with no eligible opportunities are retained and separately counted; censored is not necessarily failure."],
            "heldout_rows": heldout, "stress_rows": stress}


def write_report(report: dict, path: Path) -> dict:
    raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    compressed = gzip.compress(raw, mtime=0)
    full_path = path.with_name(path.stem + "_full.json.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(compressed)
    row_keys = {"tuning_rows", "heldout_rows", "stress_rows"}
    summary = {key: value for key, value in report.items() if key not in row_keys}
    summary["full_report"] = {"file": full_path.name, "sha256": hashlib.sha256(compressed).hexdigest(),
                              "uncompressed_bytes": len(raw), "gzip_bytes": len(compressed),
                              **{key + "_count": len(report[key]) for key in row_keys if key in report}}
    path.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    calibration = sub.add_parser("tune")
    calibration.add_argument("--output", type=Path, required=True)
    assessment = sub.add_parser("evaluate")
    assessment.add_argument("--selection", type=Path, required=True)
    assessment.add_argument("--output", type=Path, required=True)
    assessment.add_argument("--measure-time", action="store_true")
    args = parser.parse_args()
    progress = lambda split, done, count: print(f"{split}: {done}/{count}", file=sys.stderr)
    if args.command == "tune":
        report = tune(progress=progress)
    else:
        selection = json.loads(args.selection.read_text(encoding="utf-8"))
        report = evaluate(selection, measure_time=args.measure_time, progress=progress)
    summary = write_report(report, args.output)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
