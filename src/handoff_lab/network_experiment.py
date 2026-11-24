"""Real framed loopback TCP study under declared application pacing/holdoff.

No dummy buffers, external service, model calls, or production-network claims.
Fixture values and hashes are prepared outside the timer, freshly per run;
transfer encoding, policy work, receiver reconstruction and all waits are timed.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import gzip
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import queue
import random
import socket
import statistics
import struct
import sys
import threading
import time

from .adaptive_transport import (Artifact, Ledger, OnlinePolicy, View,
                                 _decode_transfer, _recovery, cheapest)
from .protocol import MAX_MESSAGE_BYTES, Delta, Envelope, ProtocolError, Ref, canonical, digest, nbytes, parse_json
from .recovery_experiment import RecentReprobe, RecoverySpec, generate, inline_tx

DESIGN_COMMIT = "c557b45892f09d12d94deb10d74e91c112d4427a"
POLICIES = ("inline", "cache_delta", "original_online", "recent_reprobe")
SCHEDULES = ("healthy", "single_loss", "loss_recovery")
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Profile:
    name: str
    reply_holdoff_seconds: float
    framed_bytes_per_second: int | None


PROFILES = (Profile("unshaped", 0.0, None), Profile("holdoff5_rate1m", 0.005, 1_000_000),
            Profile("holdoff20_rate1m", 0.020, 1_000_000))


@dataclass(frozen=True)
class Case:
    payload_chars: int
    seed: int
    schedule: str
    profile: Profile
    handoffs: int = 16

    def key(self) -> str:
        return f"chars{self.payload_chars}_{self.schedule}_{self.profile.name}"

    def to_dict(self) -> dict:
        return {"payload_chars": self.payload_chars, "seed": self.seed, "schedule": self.schedule,
                "profile": dict(self.profile.__dict__), "handoffs": self.handoffs}


class FrameError(ProtocolError):
    pass


class FramedSocket:
    """Actual 4-byte-prefixed UTF-8 stream, with pre-send pacing and holdoff."""
    def __init__(self, connection: socket.socket, profile: Profile, receiver: bool):
        self.socket = connection
        self.profile = profile
        self.receiver = receiver
        self.deadline = 0.0
        self.sent_hash = hashlib.sha256()
        self.received_hash = hashlib.sha256()
        self.counts = {"sent_stream_bytes": 0, "received_stream_bytes": 0,
                       "sent_payload_bytes": 0, "received_payload_bytes": 0,
                       "sent_frames": 0, "received_frames": 0,
                       "requested_holdoff_seconds": 0.0, "observed_holdoff_wait_seconds": 0.0,
                       "requested_pacing_seconds": 0.0, "observed_pacing_wait_seconds": 0.0}

    def _send_all(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            sent = self.socket.send(remaining)
            if sent <= 0:
                raise FrameError("Socket stopped accepting bytes")
            self.sent_hash.update(remaining[:sent])
            self.counts["sent_stream_bytes"] += sent
            remaining = remaining[sent:]

    def send_frame(self, payload: str, *, reply: bool = False) -> None:
        encoded = payload.encode("utf-8")
        if not 0 < len(encoded) <= MAX_MESSAGE_BYTES:
            raise FrameError("Invalid outgoing frame length")
        frame = struct.pack("!I", len(encoded)) + encoded
        if self.receiver and reply and self.profile.reply_holdoff_seconds:
            requested = self.profile.reply_holdoff_seconds
            started = time.perf_counter()
            time.sleep(requested)  # Before GET/ACK bytes, including the final ACK.
            self.counts["requested_holdoff_seconds"] += requested
            self.counts["observed_holdoff_wait_seconds"] += time.perf_counter() - started
        rate = self.profile.framed_bytes_per_second
        if rate is None:
            self._send_all(frame)
        else:
            for offset in range(0, len(frame), 1024):
                chunk = frame[offset:offset + 1024]
                now = time.perf_counter()
                budget = len(chunk) / rate
                self.deadline = max(now, self.deadline) + budget
                requested = max(0.0, self.deadline - now)
                started = time.perf_counter()
                time.sleep(requested)  # Before every chunk, never after final bytes.
                self.counts["requested_pacing_seconds"] += budget
                self.counts["observed_pacing_wait_seconds"] += time.perf_counter() - started
                self._send_all(chunk)
        self.counts["sent_payload_bytes"] += len(encoded)
        self.counts["sent_frames"] += 1

    def _read_exact(self, length: int) -> bytes:
        parts = []
        left = length
        while left:
            chunk = self.socket.recv(left)
            if not chunk:
                raise FrameError("Connection ended in a frame")
            self.received_hash.update(chunk)
            self.counts["received_stream_bytes"] += len(chunk)
            parts.append(chunk)
            left -= len(chunk)
        return b"".join(parts)

    def recv_frame(self) -> str:
        length = struct.unpack("!I", self._read_exact(4))[0]
        if not 0 < length <= MAX_MESSAGE_BYTES:
            raise FrameError("Invalid incoming frame length")
        encoded = self._read_exact(length)
        try:
            payload = encoded.decode("utf-8")
        except UnicodeError as exc:
            raise FrameError("Frame is not UTF-8") from exc
        self.counts["received_payload_bytes"] += length
        self.counts["received_frames"] += 1
        return payload

    def snapshot(self) -> dict:
        return {**self.counts, "sent_stream_sha256": self.sent_hash.hexdigest(),
                "received_stream_sha256": self.received_hash.hexdigest()}


def lose_before(schedule: str, index: int) -> bool:
    if schedule == "healthy":
        return False
    if schedule == "single_loss":
        return index == 1
    if schedule == "loss_recovery":
        return 1 <= index <= 9
    raise ValueError("Unknown receiver loss schedule")


def prepare_artifacts(case: Case) -> tuple[Artifact, ...]:
    # New objects each call: cached_property snapshots never carry across policies.
    spec = RecoverySpec(case.payload_chars, case.handoffs, 4, 1, "delta_sync",
                        case.seed, "tuning", "healthy")
    return generate(spec).artifacts


def capabilities() -> str:
    return canonical({"kind": "capabilities", "v": 1, "topology": "delta_sync",
                      "deployment": "direct", "cache_status": False})


def decode_handoff(wire: str, index: int, cached: Artifact | None) -> tuple[Ref, Artifact | None, str]:
    message = Envelope.decode(wire)
    if (message.task != "sweep" or message.op != "update" or
            type(message.body.get("event")) is not int or message.body["event"] != index):
        raise ProtocolError("Unexpected handoff identity or order")
    if len(message.artifacts) == 1 and set(message.body) == {"event"}:
        item = message.artifacts[0]
        target = Ref.from_dict(item["ref"])
        if "value" in item:
            return target, Artifact(target, item["value"]), "inline"
        return target, cached if cached is not None and cached.ref == target else None, "reference"
    if not message.artifacts and set(message.body) == {"event", "transfer"}:
        transfer = message.body["transfer"]
        if type(transfer) is not dict or set(transfer) != {"kind", "delta", "result"} or transfer["kind"] != "delta":
            raise ProtocolError("Malformed wire delta")
        delta = Delta.from_dict(transfer["delta"])
        target = Ref.from_dict(transfer["result"])
        if target.artifact_id != delta.base.artifact_id or target.revision != delta.base.revision + 1:
            raise ProtocolError("Delta targets a non-adjacent revision")
        resolved = _decode_transfer(canonical(transfer), cached) if cached is not None and cached.ref == delta.base else None
        return target, resolved, "delta"
    raise ProtocolError("Unsupported handoff shape")


def receiver_worker(listener: socket.socket, profile: Profile, schedule: str, handoffs: int,
                    ready: threading.Event, results: queue.Queue) -> None:
    connection = None
    framed = None
    reconstructed = []
    received_digests = []
    actions = {"inline": 0, "reference": 0, "delta": 0}
    recoveries = 0
    error = None
    try:
        connection, _ = listener.accept()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.settimeout(10.0)
        framed = FramedSocket(connection, profile, receiver=True)
        ready.set()
        if parse_json(framed.recv_frame()) != parse_json(capabilities()):
            raise ProtocolError("Unsupported capabilities")
        cached = None
        for index in range(handoffs):
            wire = framed.recv_frame()
            if lose_before(schedule, index):
                cached = None  # Only this worker owns the hidden cache-loss event.
            target, resolved, action = decode_handoff(wire, index, cached)
            actions[action] += 1
            if resolved is None:
                recoveries += 1
                request = canonical({"kind": "get", "ref": target.to_dict(),
                                     "have": cached.ref.to_dict() if cached is not None else None})
                framed.send_frame(request, reply=True)
                resolved = _decode_transfer(framed.recv_frame(), cached)
            if resolved.ref != target or digest(resolved.value) != target.sha256:
                raise ProtocolError("Receiver reconstructed the wrong value")
            cached = resolved
            # These are reconstructed wire values, never supplied expected fixtures.
            reconstructed.append(copy.deepcopy(resolved.value))
            received_digests.append(digest(resolved.value))
            framed.send_frame(canonical({"kind": "ack", "ref": target.to_dict(), "retained": True}), reply=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        ready.set()
        if connection is not None:
            connection.close()
        results.put({"error": error, "values": reconstructed, "digests": received_digests,
                     "actions": actions, "recoveries": recoveries,
                     "transport": framed.snapshot() if framed is not None else None})


def parse_reply(wire: str, current: Artifact, known: Artifact | None) -> tuple[str, Artifact | None]:
    value = parse_json(wire)
    if type(value) is not dict:
        raise ProtocolError("Reply must be an object")
    if value.get("kind") == "ack" and set(value) == {"kind", "ref", "retained"}:
        if value["retained"] is not True or Ref.from_dict(value["ref"]) != current.ref:
            raise ProtocolError("Invalid acknowledgment")
        return "ack", None
    if value.get("kind") == "get" and set(value) == {"kind", "ref", "have"}:
        if Ref.from_dict(value["ref"]) != current.ref:
            raise ProtocolError("Recovery request targets the wrong artifact")
        if value["have"] is None:
            return "get", None
        have = Ref.from_dict(value["have"])
        if known is None or have != known.ref:
            raise ProtocolError("Unsupported recovery base")
        return "get", known
    raise ProtocolError("Malformed receiver reply")


def run_case(case: Case, policy: str) -> dict:
    if policy not in POLICIES:
        raise ValueError("Unknown network policy")
    prep_started = time.perf_counter()
    artifacts = prepare_artifacts(case)
    preparation_seconds = time.perf_counter() - prep_started
    original, recent = OnlinePolicy(0.0), RecentReprobe(4, 4)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.settimeout(10.0)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    ready = threading.Event()
    result_queue = queue.Queue(maxsize=1)
    worker = threading.Thread(target=receiver_worker,
                              args=(listener, case.profile, case.schedule, case.handoffs, ready, result_queue),
                              daemon=True)
    connection = None
    framed = None
    started = None
    setup_seconds = None
    completion_seconds = None
    decision_seconds = 0.0
    error = None
    records = []
    total_ledger = Ledger()
    forced_count = 0
    setup_started = time.perf_counter()
    worker.start()
    try:
        connection = socket.create_connection(listener.getsockname(), timeout=10.0)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.settimeout(10.0)
        if not ready.wait(10.0):
            raise TimeoutError("Receiver did not become ready")
        setup_seconds = time.perf_counter() - setup_started
        framed = FramedSocket(connection, case.profile, receiver=False)
        known = None
        started = time.perf_counter()
        framed.send_frame(capabilities())
        for index, current in enumerate(artifacts):
            view = View("delta_sync", "direct", known, None)
            decision_started = time.perf_counter()
            forced = False
            if policy == "inline":
                tx = inline_tx(current, index)
            elif policy == "cache_delta":
                tx, _, _ = cheapest(current, index, view, True)
            elif policy == "original_online":
                tx = original.choose(current, index, view)
            else:
                tx, forced, _ = recent.choose(current, index, view)
            decision_seconds += time.perf_counter() - decision_started
            transfer_started = time.perf_counter()
            framed.send_frame(tx.wire)
            reply_wire = framed.recv_frame()
            reply, base = parse_reply(reply_wire, current, known)
            ledger = Ledger(message_bytes=nbytes(tx.wire))
            if reply == "get":
                ledger.cache_misses = 1
                ledger.recovery_request_bytes = nbytes(reply_wire)
                response = _recovery(current, base, view, True)
                ledger.recovery_response_bytes = nbytes(response)
                framed.send_frame(response)
                reply_wire = framed.recv_frame()
                reply, _ = parse_reply(reply_wire, current, known)
            if reply != "ack":
                raise ProtocolError("Expected final acknowledgment")
            ledger.receiver_ack_bytes = nbytes(reply_wire)
            transfer_seconds = time.perf_counter() - transfer_started
            decision_started = time.perf_counter()
            if policy == "original_online":
                original.observe(current, index, view, tx, ledger)
            elif policy == "recent_reprobe":
                recent.observe(current, view, tx, ledger)
            decision_seconds += time.perf_counter() - decision_started
            known = current
            forced_count += int(forced)
            for key, value in ledger.__dict__.items():
                setattr(total_ledger, key, getattr(total_ledger, key) + value)
            records.append({"index": index, "action": tx.kind, "forced_reprobe": forced,
                            "transfer_seconds": transfer_seconds, **ledger.to_dict(),
                            "data_payload_sha256": hashlib.sha256(tx.wire.encode()).hexdigest()})
        completion_seconds = time.perf_counter() - started
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if started is not None:
            completion_seconds = time.perf_counter() - started
    finally:
        teardown_started = time.perf_counter()
        if connection is not None:
            connection.close()
        listener.close()
        worker.join(timeout=12.0)
        teardown_seconds = time.perf_counter() - teardown_started
    received = result_queue.get_nowait() if not result_queue.empty() else {"error": "Receiver did not finish", "transport": None}
    if received["error"] is not None:
        error = error or received["error"]
    sender = framed.snapshot() if framed is not None else None
    receiver = received.get("transport")
    exact = False
    if error is None:
        exact = received["values"] == [artifact.value for artifact in artifacts]
        checks = (
            exact,
            sender["sent_stream_bytes"] == receiver["received_stream_bytes"],
            sender["received_stream_bytes"] == receiver["sent_stream_bytes"],
            sender["sent_stream_sha256"] == receiver["received_stream_sha256"],
            sender["received_stream_sha256"] == receiver["sent_stream_sha256"],
            received["recoveries"] == total_ledger.cache_misses,
            sender["sent_payload_bytes"] + sender["received_payload_bytes"] == total_ledger.total_bytes + nbytes(capabilities()),
        )
        if not all(checks):
            error = "Post-run receiver value or stream-accounting mismatch"
    frames = sender["sent_frames"] + sender["received_frames"] if sender else 0
    stream_bytes = sender["sent_stream_bytes"] + sender["received_stream_bytes"] if sender else 0
    payload_bytes = sender["sent_payload_bytes"] + sender["received_payload_bytes"] if sender else 0
    if error is None and stream_bytes != payload_bytes + 4 * frames:
        error = "Frame prefix accounting mismatch"
    return {"case": case.to_dict(), "policy": policy, "completed": error is None,
            "error": error, "receiver_values_exact": exact,
            "artifact_preparation_seconds": preparation_seconds, "connection_setup_seconds": setup_seconds,
            "completion_seconds": completion_seconds, "teardown_seconds": teardown_seconds,
            "policy_decision_observation_seconds": decision_seconds,
            "payload_bytes": payload_bytes, "application_framing_bytes": 4 * frames,
            "framed_stream_bytes": stream_bytes, "frames": frames,
            "cache_misses": total_ledger.cache_misses, "forced_reprobes": forced_count,
            "data_frames": len(records), "recovery_requests": total_ledger.cache_misses,
            "recovery_responses": total_ledger.cache_misses, "acknowledgments": len(records),
            "sender_transport": sender, "receiver_transport": receiver,
            "receiver_output_digests": received.get("digests", []), "handoff_records": records}


def run_attempt(case: Case, policy: str) -> dict:
    """Keep even setup/harness exceptions in the grid instead of discarding rows."""
    try:
        return run_case(case, policy)
    except Exception as exc:
        return {"case": case.to_dict(), "policy": policy, "completed": False,
                "error": f"Driver-caught {type(exc).__name__}: {exc}",
                "failure_stage": "setup_or_harness_exception", "receiver_values_exact": False,
                "artifact_preparation_seconds": None, "connection_setup_seconds": None,
                "completion_seconds": None, "teardown_seconds": None,
                "policy_decision_observation_seconds": None, "payload_bytes": None,
                "application_framing_bytes": None, "framed_stream_bytes": None, "frames": None,
                "cache_misses": None, "forced_reprobes": None, "data_frames": None,
                "recovery_requests": None, "recovery_responses": None, "acknowledgments": None,
                "sender_transport": None, "receiver_transport": None,
                "receiver_output_digests": [], "handoff_records": []}


def cases() -> list[Case]:
    return [Case(size, seed, schedule, profile) for (size, seed), schedule, profile in itertools.product(
        ((2048, 90571), (16384, 90583)), SCHEDULES, PROFILES)]


def execution_order() -> list[tuple[int, Case, str]]:
    rng = random.Random(90601)
    ordered = []
    for repetition in range(3):
        shuffled = cases()
        rng.shuffle(shuffled)
        for case in shuffled:
            policies = list(POLICIES)
            rng.shuffle(policies)
            ordered.extend((repetition, case, policy) for policy in policies)
    return ordered


def summarize(rows: list[dict]) -> list[dict]:
    groups = []
    for case in cases():
        selected = [row for row in rows if row["case"] == case.to_dict()]
        policies = {}
        for name in POLICIES:
            sample = sorted((row for row in selected if row["policy"] == name), key=lambda row: row["repetition"])
            success = [row for row in sample if row["completed"]]
            values = [row["completion_seconds"] for row in success]
            policies[name] = {"attempts": len(sample), "completed": len(success),
                "completion_seconds_by_repetition": [{"repetition": row["repetition"], "seconds": row["completion_seconds"],
                                                        "completed": row["completed"]} for row in sample],
                "median_completion_seconds": statistics.median(values) if values else None,
                "minimum_completion_seconds": min(values) if values else None,
                "maximum_completion_seconds": max(values) if values else None,
                "framed_stream_bytes": [row["framed_stream_bytes"] for row in sample],
                "cache_misses": [row["cache_misses"] for row in sample],
                "median_policy_seconds": statistics.median([row["policy_decision_observation_seconds"] for row in success]) if success else None,
                "deterministic_bytes_across_repetitions": len({row["framed_stream_bytes"] for row in success}) <= 1}
        paired = {}
        for baseline in ("cache_delta", "original_online", "inline"):
            ratios = []
            for repetition in range(3):
                a = next((row for row in selected if row["policy"] == "recent_reprobe" and row["repetition"] == repetition), None)
                b = next((row for row in selected if row["policy"] == baseline and row["repetition"] == repetition), None)
                if a is not None and b is not None and a["completed"] and b["completed"]:
                    ratios.append({"repetition": repetition, "recent_over_baseline": a["completion_seconds"] / b["completion_seconds"]})
            median_ratio = statistics.median([item["recent_over_baseline"] for item in ratios]) if ratios else None
            paired[baseline] = {"ratios": ratios, "median_ratio": median_ratio,
                                "median_completion_reduction_fraction": 1 - median_ratio if median_ratio is not None else None}
        groups.append({"case": case.to_dict(), "complete_case": len(selected) == 12 and all(row["completed"] for row in selected),
                       "policies": policies, "recent_paired_comparisons": paired})
    return groups


def experiment(*, progress=None) -> dict:
    started = time.perf_counter()
    warmup_success = []
    for index, policy in enumerate(POLICIES):
        warmup = run_case(Case(64, 90701 + index, "healthy", PROFILES[0], handoffs=2), policy)
        warmup_success.append(warmup["completed"])
    if not all(warmup_success):
        raise RuntimeError("Functional warmup failed; timing grid was not started")
    rows = []
    stopped_for_budget = False
    for order, (repetition, case, policy) in enumerate(execution_order()):
        if time.perf_counter() - started > 180:
            stopped_for_budget = True
            break
        row = run_attempt(case, policy)
        row.update({"repetition": repetition, "execution_order": order,
                    "study_elapsed_seconds_after_run": time.perf_counter() - started})
        rows.append(row)
        if progress and order % 12 == 0:
            progress(order + 1, 216, row)
    files = ("src/handoff_lab/network_experiment.py", "src/handoff_lab/recovery_experiment.py",
             "src/handoff_lab/adaptive_transport.py", "src/handoff_lab/protocol.py")
    return {"schema_version": 1, "experiment": "real_wire_controlled_transport_v1",
            "design_commit": DESIGN_COMMIT,
            "design_sha256": hashlib.sha256((ROOT / "docs/network_design.md").read_bytes()).hexdigest(),
            "implementation_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files},
            "frozen_recovery_parameters": {"window": 4, "interval": 4},
            "planned_runs": 216, "attempted_runs": len(rows), "completed_runs": sum(row["completed"] for row in rows),
            "budget_seconds": 180, "stopped_for_budget": stopped_for_budget,
            "study_wall_seconds": time.perf_counter() - started, "functional_warmups_passed": all(warmup_success),
            "environment": {"python_version": sys.version, "python_implementation": platform.python_implementation(),
                "platform": platform.platform(), "logical_cpu_count": os.cpu_count(),
                "perf_counter_implementation": time.get_clock_info("perf_counter").implementation,
                "perf_counter_resolution_seconds": time.get_clock_info("perf_counter").resolution,
                "perf_counter_monotonic": time.get_clock_info("perf_counter").monotonic},
            "model_calls_performed": 0, "ordering_seed": 90601,
            "framing": "4-byte unsigned big-endian length followed by actual UTF-8 JSON; TCP_NODELAY at both endpoints",
            "pacing": "Before chunks of at most 1024 bytes, including frame prefixes; no idle credit; holdoff before each receiver GET/ACK",
            "precomputed_outside_timer": "Fresh fixture values and initial content hashes per run; no snapshot/transfer cached_property carryover",
            "timing_boundary": "Before capability frame through final ACK validation and policy observation; TCP setup and teardown separate",
            "limitations": [
                "Local loopback TCP with imposed application pacing/reply service delay, not a real WAN or production agent system.",
                "Counted stream bytes include application length prefixes; IP/TCP/Ethernet headers and retransmissions are not captured.",
                "Exactly reconstructed values came from real received messages; no dummy buffers or expected-content side channel.",
                "Only receiver code executes hidden cache loss; policies receive the same acknowledgment/recovery history.",
                "Three repetitions are descriptive; host load, scheduling and Python overhead can affect small differences.",
                "Each run uses newly generated Artifact objects so cached serialization work cannot leak across policy order.",
                "Artificial waits are included in completion time and explicitly reported; no inference-speed or breakthrough claim."],
            "case_summaries": summarize(rows), "runs": rows}


def write_report(report: dict, path: Path) -> dict:
    raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    compressed = gzip.compress(raw, mtime=0)
    full = path.with_name(path.stem + "_full.json.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(compressed)
    summary = {key: value for key, value in report.items() if key != "runs"}
    summary["full_report"] = {"file": full.name, "sha256": hashlib.sha256(compressed).hexdigest(),
                              "gzip_bytes": len(compressed), "uncompressed_bytes": len(raw), "run_rows": len(report["runs"])}
    path.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = experiment(progress=lambda done, total, row: print(
        f"{done}/{total}: {row['case']['profile']['name']} {row['policy']} completed={row['completed']}", file=sys.stderr))
    summary = write_report(report, args.output)
    print(json.dumps({key: summary[key] for key in ("planned_runs", "attempted_runs", "completed_runs",
                                                   "stopped_for_budget", "study_wall_seconds", "full_report")}, indent=2))


if __name__ == "__main__":
    main()
