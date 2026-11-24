"""Frozen causal completion-time study; actual validated loopback TCP traffic.

Run tuning only after source publication, then publish its complete observations
and selection before evaluation. No endpoint/model service or fake payloads.
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
import math
import os
from pathlib import Path
import platform
import queue
import random
import socket
import statistics
import sys
import threading
import time

from .adaptive_transport import Artifact, _decode_transfer, _delta
from .network_experiment import (FramedSocket, Profile, capabilities,
                                 decode_handoff, parse_reply)
from .protocol import MAX_MESSAGE_BYTES, ProtocolError, Ref, canonical, digest, nbytes
from .recovery_experiment import RecoverySpec, generate

DESIGN_COMMIT = "dac8aa0837ed78bc7e9688433e15bc2fef777a3f"
ROOT = Path(__file__).resolve().parents[2]
REPETITIONS = 3
BUDGET_SECONDS = 180.0
LOSS_XOR = 0x1A2B3C4D
UNSHAPED = Profile("unshaped", 0.0, None)
TUNING_PROFILES = (UNSHAPED, Profile("holdoff12_rate750k", 0.012, 750_000))
HELDOUT_PROFILES = (UNSHAPED, Profile("holdoff4_rate400k", 0.004, 400_000),
                    Profile("holdoff16_rate1200k", 0.016, 1_200_000))


@dataclass(frozen=True)
class Case:
    split: str
    payload_chars: int
    seed: int
    schedule: str
    handoffs: int
    profile: Profile

    def key(self) -> str:
        return f"{self.split}_chars{self.payload_chars}_{self.schedule}_{self.profile.name}"

    def to_dict(self) -> dict:
        return {**self.__dict__, "profile": dict(self.profile.__dict__)}


@dataclass(frozen=True)
class PolicySpec:
    family: str
    interval: int = 0
    forgetting: float | None = None

    def __post_init__(self):
        if self.family not in {"inline", "cache_delta", "miss_guard", "recent_reprobe", "latency_model"}:
            raise ValueError("Unknown latency policy")
        if self.family in {"miss_guard", "recent_reprobe", "latency_model"}:
            if type(self.interval) is not int or self.interval < 1:
                raise ValueError("Positive probe interval required")
        elif self.interval != 0:
            raise ValueError("Fixed policies do not have a probe interval")
        if self.family == "recent_reprobe" and self.interval != 4:
            raise ValueError("The reference recent policy is frozen at W4/K4")
        if self.family == "latency_model":
            if type(self.forgetting) not in (int, float) or not 0 < self.forgetting <= 1:
                raise ValueError("Forgetting must be in (0, 1]")
        elif self.forgetting is not None:
            raise ValueError("Only the time model has a forgetting factor")

    @property
    def name(self) -> str:
        if self.family == "latency_model":
            return f"latency_model_lambda{self.forgetting:g}_K{self.interval}"
        if self.family == "recent_reprobe":
            return "recent_reprobe_W4K4"
        if self.family == "miss_guard":
            return f"miss_guard_K{self.interval}"
        return self.family

    def to_dict(self) -> dict:
        return dict(self.__dict__)


INLINE = PolicySpec("inline")
CACHE = PolicySpec("cache_delta")
RECENT = PolicySpec("recent_reprobe", 4)
GUARDS = tuple(PolicySpec("miss_guard", interval) for interval in (2, 4))
MODELS = tuple(PolicySpec("latency_model", interval, forgetting)
               for forgetting, interval in itertools.product((0.5, 0.9), (2, 4)))
TUNING_POLICIES = (INLINE, CACHE, *GUARDS, RECENT, *MODELS)


def cases(split: str) -> list[Case]:
    if split == "tuning":
        sources = ((1024, 116101), (8192, 116129))
        workloads = (("healthy", 20), ("early_recovery", 14), ("late_recovery", 20))
        profiles = TUNING_PROFILES
    elif split == "heldout":
        sources = ((1536, 127003), (12288, 127031))
        workloads = (("healthy", 24), ("short_single", 7), ("burst_recovery", 24),
                     ("intermittent_recovery", 18))
        profiles = HELDOUT_PROFILES
    else:
        raise ValueError("Unknown split")
    return [Case(split, size, seed, schedule, count, profile)
            for (size, seed), (schedule, count), profile in itertools.product(sources, workloads, profiles)]


def prepare_artifacts(case: Case) -> tuple[Artifact, ...]:
    # Fresh objects per run; no snapshot/codec state carries across policies.
    spec = RecoverySpec(case.payload_chars, case.handoffs, 5, 1, "delta_sync",
                        case.seed, "tuning", "healthy")
    return generate(spec).artifacts


def lose_before(split: str, schedule: str, index: int, draw: float) -> bool:
    if schedule == "healthy":
        return False
    if split == "tuning" and schedule == "early_recovery":
        return 1 <= index <= 5
    if split == "tuning" and schedule == "late_recovery":
        return 7 <= index <= 12
    if split == "heldout" and schedule == "short_single":
        return index == 2
    if split == "heldout" and schedule == "burst_recovery":
        return 2 <= index <= 9
    if split == "heldout" and schedule == "intermittent_recovery":
        return 1 <= index <= 10 and draw < 0.6
    raise ValueError("Unknown receiver loss schedule")


# Canonical Envelope order, with only validated canonical components inserted.
_START = '{"artifacts":['
_MIDDLE = '],"body":{"event":'
_DELTA_START = '{"artifacts":[],"body":{"event":'
_TRANSFER = ',"transfer":'
_END = '},"from":"producer","op":"update","task":"sweep","to":"consumer","v":1}'
_ORDINARY_OVERHEAD = len((_START + _MIDDLE + _END).encode())
_DELTA_OVERHEAD = len((_DELTA_START + _TRANSFER + _END).encode())


@dataclass(frozen=True)
class PacketPlan:
    """Only current sender-owned lengths; never a profile or hidden cache flag."""
    index: int
    inline_bytes: int
    cache_kind: str | None
    cache_bytes: int | None
    ack_bytes: int
    get_bytes: int | None = None
    snapshot_bytes: int | None = None

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0:
            raise ProtocolError("Invalid event index")
        for value in (self.inline_bytes, self.ack_bytes):
            if type(value) is not int or not 0 < value <= MAX_MESSAGE_BYTES:
                raise ProtocolError("Invalid quoted packet size")
        if self.cache_kind not in {None, "reference", "delta"}:
            raise ProtocolError("Invalid quoted cache action")
        if (self.cache_kind is None) != (self.cache_bytes is None):
            raise ProtocolError("Cache kind and length must agree")
        for value in (self.cache_bytes, self.get_bytes, self.snapshot_bytes):
            if value is not None and (type(value) is not int or not 0 < value <= MAX_MESSAGE_BYTES):
                raise ProtocolError("Invalid optional quoted size")
        if (self.get_bytes is None) != (self.snapshot_bytes is None):
            raise ProtocolError("Both recovery sizes are required together")

    @property
    def eligible(self) -> bool:
        return self.cache_bytes is not None and self.cache_bytes < self.inline_bytes

    def best_hit_action(self) -> str:
        options = [(self.inline_bytes, "inline")]
        if self.cache_bytes is not None:
            options.append((self.cache_bytes, self.cache_kind))
        return min(options)[1]

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class PacketCodec:
    """Lazy checked reusable content; every policy pays for its requested work."""
    def __init__(self):
        self._refs: dict[Ref, str] = {}
        self._values: dict[Ref, str] = {}
        self._parts: dict[tuple, tuple[str, int]] = {}
        self._deltas: dict[tuple[Ref, Ref], str | None] = {}
        self.materializations = {"reference": 0, "value": 0, "inline_attachment": 0,
                                 "reference_attachment": 0, "delta": 0, "snapshot": 0,
                                 "ack": 0, "get": 0}

    def _ref(self, artifact: Artifact) -> str:
        if artifact.ref not in self._refs:
            self._refs[artifact.ref] = canonical(artifact.ref.to_dict())
            self.materializations["reference"] += 1
        return self._refs[artifact.ref]

    def _value(self, artifact: Artifact) -> str:
        if artifact.ref not in self._values:
            wire = canonical(artifact.value)
            if type(artifact.value) is not dict or hashlib.sha256(wire.encode()).hexdigest() != artifact.ref.sha256:
                raise ProtocolError("Source artifact does not match its reference")
            self._values[artifact.ref] = wire
            self.materializations["value"] += 1
        return self._values[artifact.ref]

    def part(self, artifact: Artifact, kind: str) -> tuple[str, int]:
        key = (kind, artifact.ref)
        if key not in self._parts:
            ref = self._ref(artifact)
            if kind == "inline_attachment":
                wire = '{"ref":' + ref + ',"value":' + self._value(artifact) + '}'
            elif kind == "reference_attachment":
                wire = '{"ref":' + ref + '}'
            elif kind == "snapshot":
                wire = '{"kind":"snapshot","ref":' + ref + ',"value":' + self._value(artifact) + '}'
            elif kind == "ack":
                wire = '{"kind":"ack","ref":' + ref + ',"retained":true}'
            elif kind == "get":
                wire = '{"have":null,"kind":"get","ref":' + ref + '}'
            else:
                raise ProtocolError("Unknown reusable packet component")
            self._parts[key] = (wire, nbytes(wire))
            self.materializations[kind] += 1
        return self._parts[key]

    def delta(self, previous: Artifact | None, current: Artifact) -> str | None:
        if (previous is None or previous.ref.artifact_id != current.ref.artifact_id
                or previous.ref.revision + 1 != current.ref.revision):
            return None
        key = (previous.ref, current.ref)
        if key not in self._deltas:
            self._value(previous)
            self._value(current)
            self._deltas[key] = _delta(previous, current)
            self.materializations["delta"] += 1
        return self._deltas[key]

    def quote(self, current: Artifact, known: Artifact | None, index: int,
              *, compare_cache: bool, recovery_sizes: bool) -> PacketPlan:
        if type(index) is not int or index < 0:
            raise ProtocolError("Invalid event index")
        digits = len(str(index))
        inline_size = _ORDINARY_OVERHEAD + digits + self.part(current, "inline_attachment")[1]
        kind = size = None
        if compare_cache and known is not None:
            if known.ref == current.ref:
                kind = "reference"
                size = _ORDINARY_OVERHEAD + digits + self.part(current, "reference_attachment")[1]
            else:
                delta = self.delta(known, current)
                if delta is not None:
                    kind, size = "delta", _DELTA_OVERHEAD + digits + nbytes(delta)
        get_size = snapshot_size = None
        if recovery_sizes and kind is not None:
            get_size = self.part(current, "get")[1]
            snapshot_size = self.part(current, "snapshot")[1]
        return PacketPlan(index, inline_size, kind, size, self.part(current, "ack")[1],
                          get_size, snapshot_size)

    def envelope(self, current: Artifact, known: Artifact | None, index: int, action: str) -> str:
        if type(index) is not int or index < 0:
            raise ProtocolError("Invalid event index")
        if action in {"inline", "reference"}:
            part = self.part(current, f"{action}_attachment")[0]
            wire = _START + part + _MIDDLE + str(index) + _END
        elif action == "delta":
            packet = self.delta(known, current)
            if packet is None:
                raise ProtocolError("No adjacent delta is available")
            wire = _DELTA_START + str(index) + _TRANSFER + packet + _END
        else:
            raise ProtocolError("Unknown data action")
        if nbytes(wire) > MAX_MESSAGE_BYTES:
            raise ProtocolError("Selected message exceeds the frame limit")
        return wire

    def recovery(self, current: Artifact, base: Artifact | None) -> str:
        snapshot, size = self.part(current, "snapshot")
        delta = self.delta(base, current)
        return delta if delta is not None and nbytes(delta) < size else snapshot


@dataclass(frozen=True)
class Observation:
    """A completed action, built from actual parsed replies and stream counters."""
    action: str
    framed_bytes: int
    reply_count: int
    get_observed: bool
    service_seconds: float

    def __post_init__(self):
        if self.action not in {"inline", "reference", "delta"}:
            raise ProtocolError("Invalid observed action")
        if type(self.framed_bytes) is not int or self.framed_bytes <= 0:
            raise ProtocolError("Positive observed framed byte count required")
        if type(self.reply_count) is not int or self.reply_count not in {1, 2}:
            raise ProtocolError("Expected ACK or GET then ACK")
        if type(self.get_observed) is not bool or self.reply_count != 1 + self.get_observed:
            raise ProtocolError("Observed replies do not match recovery")
        if self.action == "inline" and self.get_observed:
            raise ProtocolError("A validated inline artifact cannot require recovery")
        if type(self.service_seconds) not in (int, float) or not math.isfinite(self.service_seconds) or self.service_seconds <= 0:
            raise ProtocolError("Observed service duration must be finite and positive")

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class TimeFit:
    """Two-feature EW nonnegative least squares, with the frozen fallback/ties."""
    def __init__(self, forgetting: float):
        if not math.isfinite(forgetting) or not 0 < forgetting <= 1:
            raise ValueError("Invalid forgetting factor")
        self.forgetting = forgetting
        self.rr = self.rb = self.bb = self.zr = self.zb = self.u = 0.0
        self.observations = 0

    def update(self, observation: Observation) -> None:
        r, b, t = observation.reply_count, observation.framed_bytes / 1024, observation.service_seconds
        f = self.forgetting
        values = (f * self.rr + r * r, f * self.rb + r * b, f * self.bb + b * b,
                  f * self.zr + r * t, f * self.zb + b * t, f * self.u + t * t)
        if not all(math.isfinite(value) for value in values):
            raise ProtocolError("Nonfinite time-model sufficient statistics")
        self.rr, self.rb, self.bb, self.zr, self.zb, self.u = values
        self.observations += 1

    def fit(self) -> tuple[float, float, str]:
        if not self.observations:
            return 0.0, 0.0, "empty"
        det = self.rr * self.bb - self.rb * self.rb
        a_only = max(0.0, self.zr / self.rr)
        if det <= 1e-12 * self.rr * self.bb:
            return a_only, 0.0, "underidentified_a_only"
        candidates = [(a_only, 0.0, "a_only"),
                      (0.0, max(0.0, self.zb / self.bb), "b_only")]
        a = (self.zr * self.bb - self.zb * self.rb) / det
        b = (self.zb * self.rr - self.zr * self.rb) / det
        if a >= 0 and b >= 0:
            candidates.append((a, b, "interior"))
        candidates.append((0.0, 0.0, "zero"))
        scored = []
        for a, b, label in candidates:
            score = a*a*self.rr + 2*a*b*self.rb + b*b*self.bb - 2*(a*self.zr + b*self.zb)
            if not all(math.isfinite(value) for value in (a, b, score)):
                raise ProtocolError("Nonfinite fitted time-model parameter")
            scored.append((score, a, b, label))
        best = min(item[0] for item in scored)
        tolerance = 1e-12 * max(self.u, 1e-12)
        _, a, b, label = next(item for item in scored if item[0] <= best + tolerance)
        return a, b, label


@dataclass(frozen=True)
class Decision:
    action: str
    forced_probe: bool
    eligible: bool
    loss_probability: float
    a_seconds_per_reply: float | None = None
    b_seconds_per_kib: float | None = None
    fit_kind: str | None = None
    inline_score_seconds: float | None = None
    cache_score_seconds: float | None = None

    def to_dict(self) -> dict:
        value = dict(self.__dict__)
        value["predicted_selected_seconds"] = (self.inline_score_seconds if self.action == "inline"
                                                else self.cache_score_seconds)
        return value


class Controller:
    """Pure past-only policy: choose(PacketPlan), observe(Observation)."""
    def __init__(self, spec: PolicySpec):
        self.spec = spec
        self.history = deque(maxlen=4)
        self.suppressed = 0
        self.trusted = True
        self.model = TimeFit(spec.forgetting) if spec.family == "latency_model" else None

    @property
    def probability(self) -> float:
        return sum(self.history) / len(self.history) if self.history else 0.0

    @property
    def needs_recovery_sizes(self) -> bool:
        return self.spec.family in {"recent_reprobe", "latency_model"} and self.probability > 0

    def choose(self, plan: PacketPlan) -> Decision:
        family, probability = self.spec.family, self.probability
        eligible = plan.eligible
        base = plan.best_hit_action()
        action, forced = "inline", False
        a = b = fit_kind = inline_score = cache_score = None
        if family == "inline":
            pass
        elif family == "cache_delta":
            action = base
        elif family == "miss_guard":
            if self.trusted:
                action = base
            elif eligible:
                self.suppressed += 1
                if self.suppressed >= self.spec.interval:
                    action, forced = plan.cache_kind, True
        elif family == "recent_reprobe":
            if plan.cache_kind is not None:
                if probability > 0 and plan.get_bytes is None:
                    raise ProtocolError("Risk decision requires recovery lengths")
                penalty = probability * ((plan.get_bytes or 0) + (plan.snapshot_bytes or 0))
                # Preserve the frozen implementation's arithmetic order too:
                # normal(message + ACK) + p * (lost - normal).
                action = min((plan.inline_bytes + plan.ack_bytes, "inline"),
                             (plan.cache_bytes + plan.ack_bytes + penalty, plan.cache_kind))[1]
            if action == "inline" and eligible:
                self.suppressed += 1
                if self.suppressed >= self.spec.interval:
                    action, forced = plan.cache_kind, True
        elif eligible:
            if probability == 0:
                action = plan.cache_kind
            else:
                if plan.get_bytes is None:
                    raise ProtocolError("Time decision requires recovery lengths")
                a, b, fit_kind = self.model.fit()
                inline_score = a + b * (plan.inline_bytes + plan.ack_bytes + 8) / 1024
                cache_score = (a + b * (plan.cache_bytes + plan.ack_bytes + 8) / 1024
                               + probability * (a + b * (plan.get_bytes + plan.snapshot_bytes + 8) / 1024))
                if cache_score <= inline_score:
                    action = plan.cache_kind
                else:
                    self.suppressed += 1
                    if self.suppressed >= self.spec.interval:
                        action, forced = plan.cache_kind, True
        if action in {"reference", "delta"}:
            self.suppressed = 0
        return Decision(action, forced, eligible, probability, a, b, fit_kind, inline_score, cache_score)

    def observe(self, observation: Observation) -> None:
        if self.model is not None:
            self.model.update(observation)
        if observation.action in {"reference", "delta"}:
            if self.spec.family in {"recent_reprobe", "latency_model"}:
                self.history.append(int(observation.get_observed))
            if self.spec.family == "miss_guard":
                self.trusted = not observation.get_observed
                self.suppressed = 0


def receiver_worker(listener: socket.socket, profile: Profile, split: str, schedule: str,
                    loss_seed: int, handoffs: int, ready: threading.Event, results: queue.Queue) -> None:
    """No source content/seed or expected values: reconstruct only actual frames."""
    connection = framed = None
    reconstructed, output_digests, losses = [], [], []
    actions = {"inline": 0, "reference": 0, "delta": 0}
    recoveries = acknowledgments = 0
    error = None
    stage = "accept"
    try:
        connection, _ = listener.accept()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.settimeout(10.0)
        framed = FramedSocket(connection, profile, receiver=True)
        ready.set()
        stage = "capabilities"
        if framed.recv_frame() != capabilities():
            raise ProtocolError("Unsupported capabilities")
        cached = None
        loss_rng = random.Random(loss_seed)
        for index in range(handoffs):
            stage = f"event_{index}_data"
            wire = framed.recv_frame()
            lost = lose_before(split, schedule, index, loss_rng.random())
            losses.append(lost)
            if lost:
                cached = None  # Only the receiver executes this hidden loss.
            target, resolved, action = decode_handoff(wire, index, cached)
            actions[action] += 1
            if resolved is None:
                request = canonical({"kind": "get", "ref": target.to_dict(),
                                     "have": cached.ref.to_dict() if cached else None})
                stage = f"event_{index}_get"
                framed.send_frame(request, reply=True)
                recoveries += 1
                stage = f"event_{index}_recovery"
                resolved = _decode_transfer(framed.recv_frame(), cached)
            actual_hash = digest(resolved.value)
            if resolved.ref != target or actual_hash != target.sha256:
                raise ProtocolError("Receiver reconstructed the wrong artifact")
            cached = resolved
            reconstructed.append(copy.deepcopy(resolved.value))
            output_digests.append(actual_hash)
            stage = f"event_{index}_ack"
            framed.send_frame(canonical({"kind": "ack", "ref": target.to_dict(), "retained": True}), reply=True)
            acknowledgments += 1
        stage = "complete"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        ready.set()
        if connection is not None:
            connection.close()
        results.put({"error": error, "stage": stage, "values": reconstructed,
                     "output_digests": output_digests, "executed_loss_events": losses,
                     "actions": actions, "recoveries": recoveries, "acknowledgments": acknowledgments,
                     "transport": framed.snapshot() if framed else None})


def _stream_bytes(framed: FramedSocket) -> int:
    return framed.counts["sent_stream_bytes"] + framed.counts["received_stream_bytes"]


def _run_record(case: Case, spec: PolicySpec) -> dict:
    return {"case": case.to_dict(), "policy": spec.name, "policy_spec": spec.to_dict(),
            "completed": False, "missing": False, "error": None, "failure_stage": None,
            "receiver_values_exact": False, "policy_replay_exact": False,
            "artifact_preparation_seconds": None, "connection_setup_seconds": None,
            "completion_seconds": None, "teardown_seconds": None,
            "codec_quote_seconds": 0.0, "codec_construction_recovery_seconds": 0.0,
            "policy_selection_seconds": 0.0, "policy_observation_seconds": 0.0,
            "payload_bytes": None, "application_framing_bytes": None,
            "framed_stream_bytes": None, "frames": None, "capability_frames": 0,
            "data_frames": 0, "recovery_requests": 0, "recovery_responses": 0,
            "acknowledgments": 0, "forced_probes": 0,
            "sender_transport": None, "receiver_transport": None,
            "receiver_output_digests": [], "receiver_executed_loss_events": [],
            "codec_materializations": None, "handoff_records": [], "partial_event": None}


def replay_decisions(row: dict) -> bool:
    """Replay only past completed observations; hidden receiver fields are unused."""
    controller = Controller(PolicySpec(**row["policy_spec"]))
    for event in row["handoff_records"]:
        plan = PacketPlan(**event["packet_plan"])
        decision = controller.choose(plan).to_dict()
        if decision != event["decision"]:
            return False
        observation = Observation(**event["observation"])
        if observation.action != decision["action"]:
            return False
        controller.observe(observation)
    return True


def run_case(case: Case, spec: PolicySpec) -> dict:
    """One actual socket session; protected setup and partial failure accounting."""
    row = _run_record(case, spec)
    listener = connection = framed = worker = None
    worker_started = False
    ready, result_queue = threading.Event(), queue.Queue(maxsize=1)
    artifacts = ()
    codec = None
    main_started = setup_started = None
    stage = "source_preparation"
    try:
        preparation_started = time.perf_counter()
        artifacts = prepare_artifacts(case)
        row["artifact_preparation_seconds"] = time.perf_counter() - preparation_started
        controller, codec = Controller(spec), PacketCodec()
        stage = "connection_setup"
        setup_started = time.perf_counter()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.settimeout(10.0)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        worker = threading.Thread(target=receiver_worker,
            args=(listener, case.profile, case.split, case.schedule, case.seed ^ LOSS_XOR,
                  case.handoffs, ready, result_queue), daemon=True)
        worker.start()
        worker_started = True
        connection = socket.create_connection(listener.getsockname(), timeout=10.0)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.settimeout(10.0)
        if not ready.wait(10.0):
            raise TimeoutError("Receiver did not become ready")
        row["connection_setup_seconds"] = time.perf_counter() - setup_started
        framed = FramedSocket(connection, case.profile, receiver=False)
        known = None
        stage = "capabilities"
        main_started = time.perf_counter()
        framed.send_frame(capabilities())
        row["capability_frames"] = 1
        for index, current in enumerate(artifacts):
            row["partial_event"] = {"index": index}
            stage = f"event_{index}_quote"
            tick = time.perf_counter()
            plan = codec.quote(current, known, index, compare_cache=spec.family != "inline",
                               recovery_sizes=controller.needs_recovery_sizes)
            quote_seconds = time.perf_counter() - tick
            row["codec_quote_seconds"] += quote_seconds
            stage = f"event_{index}_selection"
            tick = time.perf_counter()
            decision = controller.choose(plan)
            choice_seconds = time.perf_counter() - tick
            row["policy_selection_seconds"] += choice_seconds
            row["partial_event"].update({"packet_plan": plan.to_dict(), "decision": decision.to_dict()})
            stage = f"event_{index}_construction"
            service_started = time.perf_counter()
            before_bytes = _stream_bytes(framed)
            tick = time.perf_counter()
            wire = codec.envelope(current, known, index, decision.action)
            construction_seconds = time.perf_counter() - tick
            row["codec_construction_recovery_seconds"] += construction_seconds
            expected_length = plan.inline_bytes if decision.action == "inline" else plan.cache_bytes
            if nbytes(wire) != expected_length:
                raise ProtocolError("Selected wire does not match its quoted length")
            stage = f"event_{index}_data"
            framed.send_frame(wire)
            row["data_frames"] += 1
            row["forced_probes"] += int(decision.forced_probe)
            stage = f"event_{index}_reply"
            reply_wire = framed.recv_frame()
            reply, base = parse_reply(reply_wire, current, known)
            get_observed = reply == "get"
            get_bytes = recovery_bytes = 0
            if get_observed:
                row["recovery_requests"] += 1
                get_bytes = nbytes(reply_wire)
                stage = f"event_{index}_recovery_construction"
                tick = time.perf_counter()
                response = codec.recovery(current, base)
                recovery_seconds = time.perf_counter() - tick
                construction_seconds += recovery_seconds
                row["codec_construction_recovery_seconds"] += recovery_seconds
                recovery_bytes = nbytes(response)
                stage = f"event_{index}_recovery_send"
                framed.send_frame(response)
                row["recovery_responses"] += 1
                stage = f"event_{index}_final_ack"
                reply_wire = framed.recv_frame()
                reply, _ = parse_reply(reply_wire, current, known)
            if reply != "ack":
                raise ProtocolError("Expected a final acknowledgment")
            service_seconds = time.perf_counter() - service_started
            row["acknowledgments"] += 1
            observation = Observation(decision.action, _stream_bytes(framed) - before_bytes,
                                      1 + int(get_observed), get_observed, service_seconds)
            stage = f"event_{index}_observation"
            tick = time.perf_counter()
            controller.observe(observation)
            observe_seconds = time.perf_counter() - tick
            row["policy_observation_seconds"] += observe_seconds
            known = current
            row["handoff_records"].append({"index": index, "packet_plan": plan.to_dict(),
                "decision": decision.to_dict(), "observation": observation.to_dict(),
                "codec_quote_seconds": quote_seconds, "policy_selection_seconds": choice_seconds,
                "codec_construction_recovery_seconds": construction_seconds,
                "policy_observation_seconds": observe_seconds,
                "data_payload_bytes": nbytes(wire), "get_payload_bytes": get_bytes,
                "recovery_payload_bytes": recovery_bytes, "ack_payload_bytes": nbytes(reply_wire),
                "data_payload_sha256": hashlib.sha256(wire.encode()).hexdigest()})
            row["partial_event"] = None
        row["completion_seconds"] = time.perf_counter() - main_started
        stage = "complete"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["failure_stage"] = stage
        if main_started is not None:
            row["completion_seconds"] = time.perf_counter() - main_started
        elif setup_started is not None:
            row["connection_setup_seconds"] = time.perf_counter() - setup_started
    finally:
        teardown_started = time.perf_counter()
        if connection is not None:
            connection.close()
        if listener is not None:
            listener.close()
        if worker_started:
            worker.join(timeout=12.0)
        row["teardown_seconds"] = time.perf_counter() - teardown_started
    try:
        return _finish_run_record(row, case, artifacts, codec, framed, result_queue, worker_started)
    except Exception as exc:
        row["completed"] = False
        row["error"] = f"Post-audit {type(exc).__name__}: {exc}"
        row["failure_stage"] = "post_run_audit_exception"
        return row


def _finish_run_record(row, case, artifacts, codec, framed, result_queue, worker_started):
    received = result_queue.get_nowait() if not result_queue.empty() else None
    if received is not None:
        row["receiver_transport"] = received["transport"]
        row["receiver_output_digests"] = received["output_digests"]
        row["receiver_executed_loss_events"] = received["executed_loss_events"]
        row["receiver_stage"] = received["stage"]
        row["receiver_error"] = received["error"]
        if received["error"] and row["error"] is None:
            row["error"], row["failure_stage"] = received["error"], "receiver"
    elif worker_started and row["error"] is None:
        row["error"], row["failure_stage"] = "Receiver did not finish", "receiver_join"
    if framed is not None:
        row["sender_transport"] = framed.snapshot()
        sender = row["sender_transport"]
        row["framed_stream_bytes"] = sender["sent_stream_bytes"] + sender["received_stream_bytes"]
        row["payload_bytes"] = sender["sent_payload_bytes"] + sender["received_payload_bytes"]
        row["frames"] = sender["sent_frames"] + sender["received_frames"]
        row["application_framing_bytes"] = 4 * row["frames"]
    if codec is not None:
        row["codec_materializations"] = dict(codec.materializations)
    if row["error"] is None:
        sender, receiver = row["sender_transport"], row["receiver_transport"]
        exact = received["values"] == [artifact.value for artifact in artifacts]
        row["receiver_values_exact"] = exact
        row["policy_replay_exact"] = replay_decisions(row)
        event_bytes = sum(event["observation"]["framed_bytes"] for event in row["handoff_records"])
        checks = (
            exact, row["policy_replay_exact"],
            row["acknowledgments"] == case.handoffs == len(row["handoff_records"]),
            sender["sent_stream_bytes"] == receiver["received_stream_bytes"],
            sender["received_stream_bytes"] == receiver["sent_stream_bytes"],
            sender["sent_stream_sha256"] == receiver["received_stream_sha256"],
            sender["received_stream_sha256"] == receiver["sent_stream_sha256"],
            row["recovery_requests"] == row["recovery_responses"] == received["recoveries"],
            row["acknowledgments"] == received["acknowledgments"],
            row["frames"] == 1 + 2 * case.handoffs + 2 * row["recovery_requests"],
            row["framed_stream_bytes"] == row["payload_bytes"] + row["application_framing_bytes"],
            row["framed_stream_bytes"] == event_bytes + nbytes(capabilities()) + 4,
        )
        if not all(checks):
            row["error"], row["failure_stage"] = "Value, replay, or stream-accounting mismatch", "post_run_audit"
        row["completed"] = row["error"] is None
    return row


def run_attempt(case: Case, spec: PolicySpec) -> dict:
    try:
        return run_case(case, spec)
    except Exception as exc:
        row = _run_record(case, spec)
        row["error"] = f"Driver-caught {type(exc).__name__}: {exc}"
        row["failure_stage"] = "driver_or_post_run_audit"
        return row


def policies_for(split: str, selection: dict | None = None) -> tuple[PolicySpec, ...]:
    if split == "tuning":
        return TUNING_POLICIES
    if split == "heldout" and selection is not None:
        guard = PolicySpec(**selection["miss_guard"])
        model = PolicySpec(**selection["model"])
        if guard not in GUARDS or model not in MODELS:
            raise ValueError("Selection is outside the frozen candidates")
        return INLINE, CACHE, guard, RECENT, model
    raise ValueError("Held-out execution requires a frozen selection")


def execution_order(split: str, selection: dict | None = None) -> list[tuple[int, Case, PolicySpec]]:
    rng = random.Random(116189 if split == "tuning" else 127081)
    ordered = []
    for repetition in range(REPETITIONS):
        shuffled = cases(split)
        rng.shuffle(shuffled)
        for case in shuffled:
            policies = list(policies_for(split, selection))
            rng.shuffle(policies)
            ordered.extend((repetition, case, spec) for spec in policies)
    return ordered


def paired_comparison(rows: list[dict], case: Case, numerator: str, denominator: str) -> dict:
    ratios, excess = [], []
    selected = [row for row in rows if row["case"] == case.to_dict()]
    for repetition in range(REPETITIONS):
        a = [row for row in selected if row["policy"] == numerator and row["repetition"] == repetition]
        b = [row for row in selected if row["policy"] == denominator and row["repetition"] == repetition]
        if len(a) != 1 or len(b) != 1 or not a[0]["completed"] or not b[0]["completed"]:
            ratios.append({"repetition": repetition, "ratio": None, "excess_seconds": None})
        else:
            ratio = a[0]["completion_seconds"] / b[0]["completion_seconds"]
            difference = a[0]["completion_seconds"] - b[0]["completion_seconds"]
            ratios.append({"repetition": repetition, "ratio": ratio, "excess_seconds": difference})
            excess.append(difference)
    complete = all(item["ratio"] is not None for item in ratios)
    return {"numerator": numerator, "denominator": denominator, "complete": complete,
            "pairs": ratios,
            "median_paired_ratio": statistics.median(item["ratio"] for item in ratios) if complete else None,
            "median_paired_excess_seconds": statistics.median(excess) if complete else None}


def aggregate_comparison(rows: list[dict], case_list: list[Case], numerator: str, denominator: str) -> dict:
    comparisons = [{"case": case.to_dict(), **paired_comparison(rows, case, numerator, denominator)}
                   for case in case_list]
    complete = bool(comparisons) and all(case["complete"] for case in comparisons)
    value = math.exp(statistics.mean(math.log(case["median_paired_ratio"]) for case in comparisons)) if complete else None
    return {"numerator": numerator, "denominator": denominator, "complete": complete,
            "equal_case_geometric_mean_paired_ratio": value, "case_comparisons": comparisons}


def tuning_selection(rows: list[dict]) -> dict:
    scores = {}
    for spec in TUNING_POLICIES:
        result = aggregate_comparison(rows, cases("tuning"), spec.name, CACHE.name)
        scores[spec.name] = result["equal_case_geometric_mean_paired_ratio"]
    valid_guards = [spec for spec in GUARDS if scores[spec.name] is not None]
    valid_models = [spec for spec in MODELS if scores[spec.name] is not None]
    if not valid_guards or not valid_models or scores[CACHE.name] is None:
        return {"selection": None, "error": "No complete valid model/guard/cache selection", "scores": scores}
    guard = min(valid_guards, key=lambda spec: (scores[spec.name], spec.interval))
    model = min(valid_models, key=lambda spec: (scores[spec.name], spec.interval, -spec.forgetting))
    simple = [spec for spec in (CACHE, INLINE, guard) if scores[spec.name] is not None]
    baseline = min(enumerate(simple), key=lambda pair: (scores[pair[1].name], pair[0]))[1]
    return {"selection": {"miss_guard": guard.to_dict(), "model": model.to_dict(),
                           "simple_baseline": baseline.name}, "error": None, "scores": scores}


def case_summaries(rows: list[dict], split: str, policies: tuple[PolicySpec, ...]) -> list[dict]:
    summaries = []
    for case in cases(split):
        selected = [row for row in rows if row["case"] == case.to_dict()]
        detail = {}
        for spec in policies:
            samples = sorted((row for row in selected if row["policy"] == spec.name),
                             key=lambda row: row["repetition"])
            complete = len(samples) == REPETITIONS and all(row["completed"] for row in samples)
            good = [row for row in samples if row["completed"]]
            def median(key):
                return statistics.median(row[key] for row in good) if good else None
            detail[spec.name] = {"attempts": len(samples), "completed": len(good), "complete": complete,
                "measurements": [{"repetition": row["repetition"], "completed": row["completed"],
                    "missing": row["missing"], "error": row["error"],
                    "completion_seconds": row["completion_seconds"], "framed_stream_bytes": row["framed_stream_bytes"],
                    "recovery_requests": row["recovery_requests"], "forced_probes": row["forced_probes"]}
                    for row in samples],
                "median_completion_seconds": median("completion_seconds"),
                "median_codec_quote_seconds": median("codec_quote_seconds"),
                "median_codec_construction_recovery_seconds": median("codec_construction_recovery_seconds"),
                "median_policy_selection_seconds": median("policy_selection_seconds"),
                "median_policy_observation_seconds": median("policy_observation_seconds"),
                "actions_by_repetition": [{"repetition": row["repetition"],
                    "actions": [event["decision"]["action"] for event in row["handoff_records"]]}
                    for row in samples]}
        summaries.append({"case": case.to_dict(), "policies": detail,
                          "complete": all(value["complete"] for value in detail.values())})
    return summaries


def heldout_analysis(rows: list[dict], selection: dict) -> dict:
    policies = policies_for("heldout", selection)
    model = PolicySpec(**selection["model"])
    guard = PolicySpec(**selection["miss_guard"])
    simple_names = (INLINE.name, CACHE.name, guard.name)
    comparisons = {spec.name: aggregate_comparison(rows, cases("heldout"), model.name, spec.name)
                   for spec in policies if spec != model}
    simple_baseline = selection["simple_baseline"]
    primary = comparisons[simple_baseline]["equal_case_geometric_mean_paired_ratio"]
    summaries = case_summaries(rows, "heldout", policies)
    healthy, hindsight = [], []
    for group in summaries:
        case = Case(**{**group["case"], "profile": Profile(**group["case"]["profile"])})
        if case.schedule == "healthy":
            pair = paired_comparison(rows, case, model.name, CACHE.name)
            cache_median = group["policies"][CACHE.name]["median_completion_seconds"]
            allowance = max(0.0005, 0.05 * cache_median) if cache_median is not None else None
            healthy.append({"case": case.to_dict(), "allowance_seconds": allowance, **pair,
                "within_gate": pair["complete"] and pair["median_paired_excess_seconds"] <= allowance})
        complete = (group["policies"][model.name]["complete"]
                    and all(group["policies"][name]["complete"] for name in simple_names))
        if complete:
            best = min(simple_names, key=lambda name: group["policies"][name]["median_completion_seconds"])
            ratio = (group["policies"][model.name]["median_completion_seconds"]
                     / group["policies"][best]["median_completion_seconds"])
        else:
            best = ratio = None
        hindsight.append({"case": case.to_dict(), "complete": complete,
            "hindsight_whole_session_policy": best, "ratio_of_median_completion_times": ratio})
    expected = execution_order("heldout", selection)
    observed_keys = [(row["repetition"], canonical(row["case"]), row["policy"]) for row in rows]
    expected_keys = [(rep, canonical(case.to_dict()), spec.name) for rep, case, spec in expected]
    all_correct = (len(observed_keys) == len(set(observed_keys)) and set(observed_keys) == set(expected_keys)
                   and all(row["completed"] and row["receiver_values_exact"] and row["policy_replay_exact"] for row in rows))
    parity = {name: comparisons[name]["equal_case_geometric_mean_paired_ratio"] is not None
                    and comparisons[name]["equal_case_geometric_mean_paired_ratio"] <= 1.0
              for name in simple_names}
    worst = max(item["ratio_of_median_completion_times"] for item in hindsight) if all(item["complete"] for item in hindsight) else None
    gates = {"all_planned_runs_correct": all_correct,
             "five_percent_vs_validation_selected_simple": primary is not None and primary <= 0.95,
             "parity_or_better_vs_each_simple": all(parity.values()),
             "healthy_overhead_bounded": all(item["within_gate"] for item in healthy),
             "worst_hindsight_ratio_at_most_1p25": worst is not None and worst <= 1.25}
    errors = []
    for row in rows:
        if row["policy"] != model.name or not row["completed"]:
            continue
        for event in row["handoff_records"]:
            predicted = event["decision"]["predicted_selected_seconds"]
            if predicted is not None:
                errors.append({"case": row["case"], "repetition": row["repetition"], "index": event["index"],
                    "forced_probe": event["decision"]["forced_probe"], "action": event["decision"]["action"],
                    "predicted_seconds": predicted, "observed_service_seconds": event["observation"]["service_seconds"],
                    "absolute_error_seconds": abs(predicted - event["observation"]["service_seconds"])})
    return {"selected_model": model.name, "validation_selected_simple_baseline": simple_baseline,
            "primary_equal_case_geometric_mean_paired_ratio": primary,
            "model_comparisons": comparisons, "simple_parity_checks": parity,
            "healthy_controls": healthy, "hindsight_whole_session_comparisons": hindsight,
            "worst_hindsight_ratio_of_medians": worst,
            "gates": gates, "all_engineering_gates_met": all(gates.values()),
            "prediction_error_observations": len(errors),
            "mean_absolute_service_prediction_error_seconds": statistics.mean(item["absolute_error_seconds"] for item in errors) if errors else None,
            "prediction_errors": errors}


def source_hashes() -> dict:
    files = ("docs/latency_design.md", "src/handoff_lab/protocol.py",
             "src/handoff_lab/adaptive_transport.py", "src/handoff_lab/recovery_experiment.py",
             "src/handoff_lab/network_experiment.py", "src/handoff_lab/latency_experiment.py",
             "tests/test_latency_experiment.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def environment() -> dict:
    clock = time.get_clock_info("perf_counter")
    return {"python": sys.version, "platform": platform.platform(), "cpu_count": os.cpu_count(),
            "perf_counter": {key: getattr(clock, key) for key in ("implementation", "monotonic", "adjustable", "resolution")}}


def _commit(value: str) -> str:
    if type(value) is not str or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("A full lowercase 40-character published commit SHA is required")
    return value


def run_batch(split: str, *, source_commit: str, selection: dict | None = None,
              selection_commit: str | None = None, progress=None) -> dict:
    source_commit = _commit(source_commit)
    if split == "heldout":
        _commit(selection_commit)
    started = time.perf_counter()
    ordered = execution_order(split, selection)
    policies = policies_for(split, selection)
    report = {"experiment": "causal_completion_time_v1", "split": split,
        "design_commit": DESIGN_COMMIT, "source_commit": source_commit,
        "selection_commit": selection_commit, "source_hashes": source_hashes(),
        "environment": environment(), "planned_runs": len(ordered), "repetitions": REPETITIONS,
        "budget_seconds": BUDGET_SECONDS, "selection": selection,
        "metric": "equal-case geometric mean of median paired completion-time ratios",
        "timing_scope": "capabilities through final ACK validation and final policy observation/bookkeeping",
        "transport_scope": "actual loopback TCP; imposed application pacing and receiver reply holdoff",
        "counter_scope": "actual framed application stream, not TCP/IP/Ethernet headers",
        "partial_counter_note": "stream bytes include partial frames; payload/frame counters count completed frames",
        "runs": [], "warmups": []}
    warmup_failed = False
    for index, spec in enumerate(policies):
        warmup = run_attempt(Case(split, 64, 138001 + index, "healthy", 2, UNSHAPED), spec)
        report["warmups"].append(warmup)
        warmup_failed = warmup_failed or not warmup["completed"]
    for ordinal, (repetition, case, spec) in enumerate(ordered):
        if warmup_failed or time.perf_counter() - started >= BUDGET_SECONDS:
            row = _run_record(case, spec)
            row["missing"] = True
            row["error"] = "Functional warmup failed" if warmup_failed else "Batch execution budget exhausted"
            row["failure_stage"] = "not_started"
        else:
            row = run_attempt(case, spec)
        row.update({"execution_ordinal": ordinal, "repetition": repetition})
        report["runs"].append(row)
        if progress is not None and (ordinal + 1) % 18 == 0:
            progress({"processed": ordinal + 1, "planned": len(ordered),
                      "elapsed_seconds": time.perf_counter() - started,
                      "completed": sum(item["completed"] for item in report["runs"])})
    report["elapsed_execution_seconds"] = time.perf_counter() - started
    report["completed_runs"] = sum(row["completed"] for row in report["runs"])
    report["missing_runs"] = sum(row["missing"] for row in report["runs"])
    report["failed_runs"] = len(ordered) - report["completed_runs"] - report["missing_runs"]
    report["all_runs_correct"] = report["completed_runs"] == len(ordered)
    report["case_summaries"] = case_summaries(report["runs"], split, policies)
    if split == "tuning":
        report["tuning"] = tuning_selection(report["runs"])
        report["selection"] = report["tuning"]["selection"]
    else:
        report["heldout_analysis"] = heldout_analysis(report["runs"], selection)
    return report


def write_report(report: dict, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    full_path = path.with_name(path.stem + "_full.json.gz")
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    full_path.write_bytes(compressed)
    summary = {key: value for key, value in report.items() if key not in {"runs", "warmups"}}
    if "heldout_analysis" in summary:
        summary["heldout_analysis"] = {key: value for key, value in summary["heldout_analysis"].items()
                                       if key != "prediction_errors"}
    summary["warmup_status"] = [{"policy": row["policy"], "completed": row["completed"], "error": row["error"]}
                                for row in report.get("warmups", [])]
    summary["full_report"] = {"file": full_path.name, "sha256": hashlib.sha256(compressed).hexdigest(),
                              "uncompressed_bytes": len(raw), "compressed_bytes": len(compressed)}
    path.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return summary


def load_selection(path: Path) -> tuple[dict, str]:
    summary = json.loads(path.read_text())
    if summary.get("experiment") != "causal_completion_time_v1" or summary.get("split") != "tuning":
        raise ValueError("Expected this experiment's tuning summary")
    metadata = summary["full_report"]
    filename = metadata["file"]
    if Path(filename).name != filename:
        raise ValueError("The full report must be next to its summary")
    packed = path.with_name(filename).read_bytes()
    if hashlib.sha256(packed).hexdigest() != metadata["sha256"]:
        raise ValueError("Tuning full-report checksum mismatch")
    full = json.loads(gzip.decompress(packed))
    if (summary["source_hashes"] != source_hashes() or full["source_hashes"] != summary["source_hashes"]
            or full["source_commit"] != summary["source_commit"] or full["design_commit"] != DESIGN_COMMIT):
        raise ValueError("Frozen source/design provenance changed after tuning")
    selected = tuning_selection(full["runs"])
    if selected["selection"] is None or selected != full["tuning"] or selected["selection"] != summary["selection"]:
        raise ValueError("Tuning selection is absent or does not reproduce")
    return selected["selection"], _commit(summary["source_commit"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("tune", "evaluate"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", help="Published reviewed implementation SHA; required for tune")
    parser.add_argument("--selection", type=Path, help="Frozen tuning summary; required for evaluate")
    parser.add_argument("--selection-commit", help="Published tuning-output/selection SHA; required for evaluate")
    args = parser.parse_args()
    if args.stage == "tune":
        if not args.source_commit:
            parser.error("tune requires --source-commit after implementation publication")
        report = run_batch("tuning", source_commit=args.source_commit,
                           progress=lambda update: print(json.dumps(update), flush=True))
    else:
        if args.selection is None or not args.selection_commit:
            parser.error("evaluate requires --selection and --selection-commit after tuning publication")
        selection, source_commit = load_selection(args.selection)
        report = run_batch("heldout", source_commit=source_commit, selection=selection,
                           selection_commit=args.selection_commit,
                           progress=lambda update: print(json.dumps(update), flush=True))
    summary = write_report(report, args.output)
    print(json.dumps({"output": str(args.output), "completed": report["completed_runs"],
                      "planned": report["planned_runs"], "full_report": summary["full_report"]}), flush=True)


if __name__ == "__main__":
    main()


