"""Measured byte accounting for online cache admission and delta transport.

This is a checked transport simulator, not a model benchmark or a new caching
algorithm. Policies see current/past artifacts and acknowledged cache state;
only the transport knows whether a cache was silently lost before delivery.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import itertools
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from .protocol import Delta, Envelope, ProtocolError, Ref, attachment, canonical, digest, nbytes, parse_json

TOPOLOGIES = ("no_cache", "revision_cache", "delta_sync")
DEPLOYMENTS = ("direct", "external_store")
FIXED_POLICIES = ("inline", "cache_snapshot", "cache_delta", "inline_once_cache")
THRESHOLDS = (0.0, 0.5, 1.0, 2.0, 4.0)


@dataclass(frozen=True)
class Artifact:
    ref: Ref
    value: dict

    @classmethod
    def make(cls, revision: int, value: dict) -> "Artifact":
        return cls(Ref("artifact/main", revision, digest(value)), copy.deepcopy(value))

    @cached_property
    def snapshot(self) -> str:
        return canonical({"kind": "snapshot", "ref": self.ref.to_dict(), "value": self.value})


@dataclass(frozen=True)
class Spec:
    payload_chars: int
    uses: int
    update_every: int
    changed_fields: int
    topology: str
    deployment: str
    loss_probability: float = 0.0
    seed: int = 0

    def __post_init__(self):
        if (self.payload_chars < 16 or self.uses < 1 or self.update_every < 0 or
                not 0 <= self.changed_fields <= 16 or self.topology not in TOPOLOGIES or
                self.deployment not in DEPLOYMENTS or not 0 <= self.loss_probability <= 1):
            raise ValueError("Invalid workload specification")
        if self.update_every and not self.changed_fields:
            raise ValueError("Updating workloads need at least one changed field")

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Workload:
    spec: Spec
    artifacts: tuple[Artifact, ...]
    losses: tuple[bool, ...]


def generate(spec: Spec) -> Workload:
    rng = random.Random(spec.seed)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    lengths = [spec.payload_chars // 16 + int(i < spec.payload_chars % 16) for i in range(16)]
    value = {f"part{i:02}": "".join(rng.choices(alphabet, k=length)) for i, length in enumerate(lengths)}
    current = Artifact.make(1, value)
    artifacts = []
    losses = []
    for index in range(spec.uses):
        if index and spec.update_every and index % spec.update_every == 0:
            value = copy.deepcopy(current.value)
            for part in rng.sample(range(16), spec.changed_fields):
                key = f"part{part:02}"
                # A guaranteed change of the same length, including tiny payloads.
                old = value[key]
                first = alphabet[(alphabet.index(old[0]) + 1) % len(alphabet)]
                value[key] = first + "".join(rng.choices(alphabet, k=len(old) - 1))
            current = Artifact.make(current.ref.revision + 1, value)
        artifacts.append(current)
        losses.append(bool(index and rng.random() < spec.loss_probability))
    return Workload(spec, tuple(artifacts), tuple(losses))


def _delta(previous: Artifact | None, current: Artifact) -> str | None:
    if previous is None or previous.ref.revision + 1 != current.ref.revision:
        return None
    changed = {key: value for key, value in current.value.items()
               if key not in previous.value or previous.value[key] != value}
    removed = tuple(sorted(set(previous.value) - set(current.value)))
    if not changed and not removed:
        return None
    delta = Delta(previous.ref, changed, removed)
    return canonical({"kind": "delta", "delta": delta.to_dict(), "result": current.ref.to_dict()})


def _decode_transfer(packet: str, base: Artifact | None) -> Artifact:
    value = parse_json(packet)
    if type(value) is not dict:
        raise ProtocolError("Transfer packet must be an object")
    if value.get("kind") == "snapshot" and set(value) == {"kind", "ref", "value"}:
        ref = Ref.from_dict(value["ref"])
        artifact = value["value"]
    elif value.get("kind") == "delta" and set(value) == {"kind", "delta", "result"}:
        delta = Delta.from_dict(value["delta"])
        ref = Ref.from_dict(value["result"])
        if (base is None or base.ref != delta.base or ref.artifact_id != delta.base.artifact_id
                or ref.revision != delta.base.revision + 1):
            raise ProtocolError("Delta base is missing or stale")
        artifact = copy.deepcopy(base.value)
        for key in delta.remove_fields:
            if key not in artifact:
                raise ProtocolError("Removed field is missing")
            del artifact[key]
        artifact.update(delta.set_fields)
    else:
        raise ProtocolError("Invalid transfer packet")
    if type(artifact) is not dict or digest(artifact) != ref.sha256:
        raise ProtocolError("Transfer digest mismatch")
    return Artifact(ref, artifact)


@dataclass(frozen=True)
class View:
    """Only runtime-observable state. No future length, updates, or cache losses."""
    topology: str
    deployment: str
    known_cache: Artifact | None
    published: Artifact | None


@dataclass(frozen=True)
class Transmission:
    kind: str
    wire: str
    publication: str | None
    delta_packet: str | None
    allow_delta: bool


@dataclass
class Ledger:
    message_bytes: int = 0
    store_write_bytes: int = 0
    store_ack_bytes: int = 0
    recovery_request_bytes: int = 0
    recovery_response_bytes: int = 0
    receiver_ack_bytes: int = 0
    cache_misses: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(value for key, value in self.__dict__.items() if key.endswith("_bytes"))

    def to_dict(self) -> dict:
        return {**self.__dict__, "total_bytes": self.total_bytes}


def _message(current: Artifact, index: int, inline: bool) -> str:
    item = attachment(current.ref, current.value) if inline else attachment(current.ref)
    return Envelope("sweep", "update", "producer", "consumer", {"event": index}, (item,)).encode()


def candidates(current: Artifact, index: int, view: View, allow_delta: bool) -> list[Transmission]:
    """Cache choices use identical state and transfer capabilities for all policies."""
    if view.topology == "no_cache" and view.deployment == "direct":
        return [Transmission("inline", _message(current, index, True), None, None, allow_delta)]
    if view.deployment == "direct":
        options = [Transmission("inline", _message(current, index, True), None, None, allow_delta)]
        if view.known_cache is not None and view.known_cache.ref == current.ref:
            options.append(Transmission("reference", _message(current, index, False), None, None, allow_delta))
        delta = _delta(view.known_cache, current) if allow_delta and view.topology == "delta_sync" else None
        if delta:
            wire = Envelope("sweep", "update", "producer", "consumer",
                            {"event": index, "transfer": parse_json(delta)}).encode()
            options.append(Transmission("delta", wire, None, delta, allow_delta))
        return options
    publication = None
    if view.published is None or view.published.ref != current.ref:
        publication = current.snapshot
        delta = _delta(view.published, current) if allow_delta else None
        if delta and nbytes(delta) < nbytes(publication):
            publication = delta
    return [Transmission("reference", _message(current, index, False), publication, None, allow_delta)]


def _recovery(current: Artifact, cached: Artifact | None, view: View, allow_delta: bool) -> str:
    response = current.snapshot
    delta = _delta(cached, current) if allow_delta and view.topology == "delta_sync" else None
    if delta and nbytes(delta) < nbytes(response):
        response = delta
    return response


def account(tx: Transmission, current: Artifact, view: View,
            cached: Artifact | None, *, validate: bool) -> Ledger:
    ledger = Ledger(message_bytes=nbytes(tx.wire))
    server_source = current if view.deployment == "direct" else view.published
    wire_message = None
    delta_packet = tx.delta_packet
    if validate:
        wire_message = Envelope.decode(tx.wire)
        if tx.kind in {"inline", "reference"}:
            if len(wire_message.artifacts) != 1:
                raise ProtocolError("Expected exactly one artifact in the wire message")
            item = wire_message.artifacts[0]
            if Ref.from_dict(item["ref"]) != current.ref:
                raise ProtocolError("Wire reference does not identify the requested revision")
            if ("value" in item) != (tx.kind == "inline"):
                raise ProtocolError("Wire attachment does not match its transmission kind")
        elif tx.kind == "delta":
            if "transfer" not in wire_message.body or wire_message.artifacts:
                raise ProtocolError("Delta wire message must contain the transfer in its body")
            delta_packet = canonical(wire_message.body["transfer"])
            if delta_packet != tx.delta_packet:
                raise ProtocolError("Out-of-band delta disagrees with the actual wire")
            decoded_delta = parse_json(delta_packet)
            if Ref.from_dict(decoded_delta["result"]) != current.ref:
                raise ProtocolError("Wire delta targets the wrong revision")
        else:
            raise ProtocolError("Unknown transmission kind")
    if tx.publication is not None:
        ledger.store_write_bytes = nbytes(tx.publication)
        ledger.store_ack_bytes = nbytes(canonical({"kind": "stored", "ref": current.ref.to_dict()}))
        if validate:
            decoded = _decode_transfer(tx.publication, view.published)
            if decoded.ref != current.ref:
                raise ProtocolError("Publication returned the wrong revision")
            server_source = decoded
    if validate and view.deployment == "external_store" and tx.kind != "inline":
        if server_source is None or server_source.ref != current.ref:
            raise ProtocolError("External reference has not been published")
    resolved = None
    if tx.kind == "inline":
        if validate:
            item = wire_message.artifacts[0]
            resolved = Artifact(Ref.from_dict(item["ref"]), item["value"])
    elif tx.kind == "reference" and cached is not None and cached.ref == current.ref:
        resolved = cached
    elif tx.kind == "delta" and cached is not None:
        base = Ref.from_dict(parse_json(delta_packet)["delta"]["base"])
        if cached.ref == base:
            resolved = _decode_transfer(delta_packet, cached) if validate else current
    success_without_fetch = (tx.kind == "inline" or resolved is not None)
    if not success_without_fetch:
        ledger.cache_misses = 1
        request = {"kind": "get", "ref": current.ref.to_dict(),
                   "have": cached.ref.to_dict() if cached is not None else None}
        ledger.recovery_request_bytes = nbytes(canonical(request))
        response = _recovery(server_source if validate else current, cached, view, tx.allow_delta)
        ledger.recovery_response_bytes = nbytes(response)
        if validate:
            resolved = _decode_transfer(response, cached)
    if validate:
        if resolved is None or resolved.ref != current.ref or resolved.value != current.value:
            raise ProtocolError("Receiver did not reconstruct the exact artifact")
        if digest(resolved.value) != current.ref.sha256:
            raise ProtocolError("Receiver digest mismatch")
    ledger.receiver_ack_bytes = nbytes(canonical({"kind": "ack", "ref": current.ref.to_dict(),
                                                 "retained": view.topology != "no_cache"}))
    return ledger


def cheapest(current: Artifact, index: int, view: View, allow_delta: bool,
             loss_estimate: float = 0.0) -> tuple[Transmission, float, int]:
    options = candidates(current, index, view, allow_delta)
    ranked = []
    for tx in options:
        normal = account(tx, current, view, view.known_cache, validate=False).total_bytes
        lost = account(tx, current, view, None, validate=False).total_bytes
        expected = normal + loss_estimate * (lost - normal)
        ranked.append((expected, tx.kind, tx, normal))
    expected, _, choice, normal = min(ranked, key=lambda item: (item[0], item[1]))
    return choice, expected, normal


class OnlinePolicy:
    """Past-only rent/buy heuristic; no competitive-ratio or novelty claim."""
    def __init__(self, threshold: float):
        self.threshold = threshold
        self.credit = 0.0
        self.active = False
        self.previous: Artifact | None = None
        self.probes = 0
        self.losses = 0

    def choose(self, current: Artifact, index: int, view: View) -> Transmission:
        inline = Transmission("inline", _message(current, index, True), None, None, True)
        if view.topology == "no_cache":
            return inline
        # Estimate unexpected cache loss from prior reference/delta probes only.
        probability = self.losses / self.probes if self.probes else 0.0
        shared, expected, _ = cheapest(current, index, view, True, probability)
        inline_cost = account(inline, current, view, view.known_cache, validate=False).total_bytes
        if view.deployment == "direct":
            return shared if expected < inline_cost else inline
        if self.active:
            return shared
        # A shadow steady-state cache uses only the previously observed artifact.
        shadow = View(view.topology, view.deployment, view.known_cache,
                      self.previous if self.previous is not None else current)
        _, steady, _ = cheapest(current, index, shadow, True, probability)
        entry_penalty = max(0.0, expected - steady)
        if self.credit >= self.threshold * entry_penalty and self.credit > 0:
            self.active = True
            return shared
        return inline

    def observe(self, current: Artifact, index: int, view: View,
                tx: Transmission, ledger: Ledger) -> None:
        inline = Transmission("inline", _message(current, index, True), None, None, True)
        shadow = View(view.topology, view.deployment, view.known_cache,
                      self.previous if self.previous is not None else current)
        _, _, steady = cheapest(current, index, shadow, True)
        inline_cost = account(inline, current, view, view.known_cache, validate=False).total_bytes
        self.credit += max(0, inline_cost - steady)
        # Count only attempts expected to hit/apply; compulsory cold fetches are
        # not evidence of eviction. Inline messages do not expose loss.
        expected = account(tx, current, view, view.known_cache, validate=False)
        if tx.kind in {"reference", "delta"} and expected.cache_misses == 0:
            self.probes += 1
            self.losses += ledger.cache_misses
        self.previous = current


def run(workload: Workload, policy: str, *, threshold: float = 1.0,
        switch_at: int | None = None, trace: bool = False, measure_time: bool = False) -> dict:
    if policy not in (*FIXED_POLICIES, "online", "switch_oracle_candidate"):
        raise ValueError("Unknown policy")
    spec = workload.spec
    cached = known = published = None
    online = OnlinePolicy(threshold)
    total = Ledger()
    actions = {"inline": 0, "reference": 0, "delta": 0}
    decision_ns = 0
    history = []
    activation_index = None
    capability_bytes = nbytes(canonical({"kind": "capabilities", "v": 1,
                                        "topology": spec.topology, "deployment": spec.deployment}))
    for index, current in enumerate(workload.artifacts):
        if workload.losses[index]:
            cached = None  # The policy is not told; its last acknowledgment is stale.
        view = View(spec.topology, spec.deployment, known, published)
        started = time.perf_counter_ns() if measure_time else 0
        if policy == "inline" or (policy == "inline_once_cache" and index == 0) or (
                policy == "switch_oracle_candidate" and index < switch_at):
            tx = Transmission("inline", _message(current, index, True), None, None, True)
        elif policy == "online":
            tx = online.choose(current, index, view)
        else:
            tx, _, _ = cheapest(current, index, view, policy != "cache_snapshot")
        if measure_time:
            decision_ns += time.perf_counter_ns() - started
        ledger = account(tx, current, view, cached, validate=True)
        if policy == "online":
            started = time.perf_counter_ns() if measure_time else 0
            online.observe(current, index, view, tx, ledger)
            if measure_time:
                decision_ns += time.perf_counter_ns() - started
        if tx.publication is not None:
            published = current
            if activation_index is None:
                activation_index = index
        cached = current if spec.topology != "no_cache" else None
        known = cached  # Acknowledgment is received after the charged delivery.
        actions[tx.kind] += 1
        for key, value in ledger.__dict__.items():
            setattr(total, key, getattr(total, key) + value)
        if trace:
            history.append({"index": index, "revision": current.ref.revision,
                            "action": tx.kind, "published": tx.publication is not None,
                            "silent_loss": workload.losses[index], **ledger.to_dict()})
    return {"policy": policy, **total.to_dict(), "capability_setup_bytes": capability_bytes,
            "total_bytes": total.total_bytes + capability_bytes, "actions": actions,
            "activation_index": activation_index, "all_reconstructions_exact": True,
            "decision_elapsed_nanoseconds": decision_ns if measure_time else None,
            **({"trace": history} if trace else {})}


def switch_oracle(workload: Workload) -> dict:
    """Future-aware best *single admission time*, not an unconstrained optimum."""
    choices = [run(workload, "switch_oracle_candidate", switch_at=index)
               for index in range(workload.spec.uses + 1)]
    best = min(enumerate(choices), key=lambda item: item[1]["total_bytes"])
    return {"total_bytes": best[1]["total_bytes"], "switch_at": best[0],
            "scope": "Best hindsight single switch from inline to cache_delta; future workload visible"}


def grid(split: str) -> list[Spec]:
    if split == "tuning":
        sizes, uses, periods, fields = (128, 1024, 8192), (2, 6, 12, 24), (0, 2, 6), (1, 4, 12)
        seed = 3101
    elif split == "heldout":
        sizes, uses, periods, fields = (64, 512, 4096, 32768), (1, 4, 8, 16, 32, 64), (0, 1, 4, 16), (1, 8, 16)
        seed = 8401
    else:
        raise ValueError("Unknown split")
    unique = {}
    for size, count, period, changed, topology, deployment in itertools.product(
            sizes, uses, periods, fields, TOPOLOGIES, DEPLOYMENTS):
        if period == 0 or period >= count:
            period, changed = 0, 0
        key = (size, count, period, changed, topology, deployment)
        unique[key] = Spec(*key, seed=seed)
    return list(unique.values())


def _summary(rows: list[dict], selected_fixed: dict) -> dict:
    totals = {name: sum(row["policies"][name]["total_bytes"] for row in rows)
              for name in (*FIXED_POLICIES, "online")}
    oracle = sum(min(row["policies"][name]["total_bytes"] for name in FIXED_POLICIES) for row in rows)
    selected_total = sum(row["policies"][selected_fixed[row["spec"]["deployment"] + ":" +
                           row["spec"]["topology"]]]["total_bytes"] for row in rows)
    ratios = [row["policies"]["online"]["total_bytes"] /
              min(row["policies"][name]["total_bytes"] for name in FIXED_POLICIES) for row in rows]
    timings = {}
    for name in (*FIXED_POLICIES, "online"):
        values = [row["policies"][name].get("decision_elapsed_nanoseconds") for row in rows]
        timings[name] = (sum(values) / sum(row["spec"]["uses"] for row in rows) / 1000
                         if all(value is not None for value in values) else None)
    return {"workloads": len(rows), "policy_total_bytes": totals,
            "policy_mean_decision_elapsed_microseconds_per_handoff": timings,
            "tuning_selected_fixed_total_bytes": selected_total,
            "hindsight_best_fixed_per_workload_bytes": oracle,
            "online_reduction_vs_tuning_selected_fixed_fraction": 1 - totals["online"] / selected_total,
            "online_to_hindsight_best_fixed_median_ratio": statistics.median(ratios),
            "online_to_hindsight_best_fixed_max_ratio": max(ratios),
            "online_wins_vs_best_fixed_workloads": sum(value < 1 for value in ratios),
            "online_ties_vs_best_fixed_workloads": sum(value == 1 for value in ratios),
            "online_losses_vs_best_fixed_workloads": sum(value > 1 for value in ratios)}


def experiment(*, measure_time: bool = False, progress=None) -> dict:
    tuning_specs = grid("tuning")
    tuning_scores = {value: 0 for value in THRESHOLDS}
    fixed_scores = {deployment + ":" + topology: {name: 0 for name in FIXED_POLICIES}
                    for deployment, topology in itertools.product(DEPLOYMENTS, TOPOLOGIES)}
    for index, spec in enumerate(tuning_specs):
        workload = generate(spec)
        for threshold in THRESHOLDS:
            tuning_scores[threshold] += run(workload, "online", threshold=threshold)["total_bytes"]
        for name in FIXED_POLICIES:
            fixed_scores[spec.deployment + ":" + spec.topology][name] += run(workload, name)["total_bytes"]
        if progress and index % 50 == 0:
            progress("tuning", index, len(tuning_specs))
    threshold = min(THRESHOLDS, key=lambda item: (tuning_scores[item], item))
    selected = {key: min(scores, key=lambda name: (scores[name], name))
                for key, scores in fixed_scores.items()}
    heldout = []
    test_specs = grid("heldout")
    for index, spec in enumerate(test_specs):
        workload = generate(spec)
        policies = {name: run(workload, name, threshold=threshold, measure_time=measure_time)
                    for name in (*FIXED_POLICIES, "online")}
        heldout.append({"spec": spec.to_dict(), "artifact_value_bytes": nbytes(canonical(workload.artifacts[0].value)),
                        "policies": policies})
        if progress and index % 50 == 0:
            progress("heldout", index, len(test_specs))
    # Independent stress workloads: unknown cache loss and stopping just after
    # the observed online admission. Stress choices never retune the threshold.
    stress = []
    for deployment, topology, loss in itertools.product(DEPLOYMENTS, ("revision_cache", "delta_sync"), (0.1, 0.5, 1.0)):
        spec = Spec(4096, 32, 4, 1, topology, deployment, loss, 9929)
        workload = generate(spec)
        stress.append({"name": "unannounced_cache_loss", "spec": spec.to_dict(),
                       "policies": {name: run(workload, name, threshold=threshold, measure_time=measure_time)
                                    for name in (*FIXED_POLICIES, "online")},
                       "single_switch_oracle": switch_oracle(workload)})
    for size in (512, 4096, 32768):
        spec = Spec(size, 64, 0, 0, "delta_sync", "external_store", seed=7727)
        long = generate(spec)
        observed = run(long, "online", threshold=threshold)
        cutoff = (observed["activation_index"] or 0) + 1
        if observed["activation_index"] is None:
            continue
        shorter = Workload(Spec(size, cutoff, 0, 0, "delta_sync", "external_store", seed=7727),
                           long.artifacts[:cutoff], long.losses[:cutoff])
        stress.append({"name": "stop_immediately_after_online_purchase", "spec": shorter.spec.to_dict(),
                       "policies": {name: run(shorter, name, threshold=threshold, measure_time=measure_time)
                                    for name in (*FIXED_POLICIES, "online")},
                       "single_switch_oracle": switch_oracle(shorter)})
    groups = {deployment + ":" + topology: _summary(
        [row for row in heldout if row["spec"]["deployment"] == deployment and row["spec"]["topology"] == topology], selected)
        for deployment, topology in itertools.product(DEPLOYMENTS, TOPOLOGIES)}
    return {"schema_version": 1, "experiment": "online_transport_break_even",
            "model_calls_performed": 0, "tokenizer_used": None,
            "tuning": {"workloads": len(tuning_specs), "threshold_candidates": list(THRESHOLDS),
                       "aggregate_online_bytes": {str(key): value for key, value in tuning_scores.items()},
                       "selected_threshold": threshold, "fixed_policy_scores": fixed_scores,
                       "selected_fixed_by_deployment_and_topology": selected},
            "heldout_summary": _summary(heldout, selected), "groups": groups,
            "stress_summary": _summary(stress, selected), "stress": stress,
            "assumptions": [
                "Exact UTF-8 application payload bytes; no model tokens, model accuracy or inference latency measured.",
                "All policies get identical inline-seeded consumer caches, delta support and charged acknowledgments.",
                "Direct deployment permits cache references without a store; external_store requires published recoverable snapshots.",
                "Compare policies within each deployment; direct and external_store provide different availability contracts.",
                "Inline, shared snapshots and adjacent-revision top-level deltas reconstruct identical artifact objects.",
                "Sparse full snapshots may install any source revision in this simulator; the original ArtifactStore API is unchanged.",
                "Capability setup, store writes/acks, reference recovery requests/responses and receiver acknowledgments are counted.",
                "No-cache drops contents after every handoff; other caches retain only the last artifact, unless silently lost.",
                "Policies know current/past artifacts and last acknowledged retention, never future workload length or current hidden eviction.",
                "Threshold and per-deployment/topology fixed comparator are selected only on the separate tuning grid.",
                "A per-workload best-fixed comparison and hindsight best single admission time are explicitly labeled oracles.",
                "Online rent/buy and cache-loss estimation are established heuristics; no novelty or competitive guarantee claimed.",
                "Optional perf_counter elapsed decision timings include simulator serialization/quoting and policy observation, not network or LLM time.",
                "No real requests, compression, packet framing, storage I/O, authentication or model-context costs are simulated."],
            "heldout_rows": heldout}


def main() -> None:
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measure-time", action="store_true")
    args = parser.parse_args()
    report = experiment(measure_time=args.measure_time,
                        progress=lambda split, done, total: print(f"{split}: {done}/{total}", file=sys.stderr))
    write_report(report, args.output)
    print(json.dumps({"tuning": report["tuning"], "heldout_summary": report["heldout_summary"],
                      "groups": report["groups"], "stress_summary": report["stress_summary"]}, indent=2))


def write_report(report: dict, path: Path) -> dict:
    """Readable summary plus all observations in a deterministic-header gzip file."""
    raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    full_path = path.with_name(path.stem + "_full.json.gz")
    compressed = gzip.compress(raw, mtime=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(compressed)
    summary = {key: value for key, value in report.items() if key not in {"heldout_rows", "stress"}}
    summary["full_report"] = {"file": full_path.name, "sha256": hashlib.sha256(compressed).hexdigest(),
                              "uncompressed_bytes": len(raw), "gzip_bytes": len(compressed),
                              "heldout_rows": len(report["heldout_rows"]), "stress_rows": len(report["stress"])}
    path.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    main()
