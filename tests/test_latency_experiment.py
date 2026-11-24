"""Correctness fixtures only: no tuning or held-out timing grid executes here."""
import copy
import gzip
import hashlib
import inspect
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from handoff_lab.adaptive_transport import Artifact, View, _decode_transfer, _message, account
from handoff_lab.latency_experiment import (CACHE, DESIGN_COMMIT, GUARDS, HELDOUT_PROFILES,
    INLINE, MODELS, RECENT, TUNING_POLICIES, UNSHAPED, Case, Controller, Observation,
    PacketCodec, PacketPlan, PolicySpec, Profile, TimeFit, _run_record, aggregate_comparison,
    cases, execution_order, heldout_analysis, load_selection, lose_before, paired_comparison,
    policies_for, prepare_artifacts, replay_decisions, run_attempt, run_batch, run_case,
    source_hashes, tuning_selection, write_report)
from handoff_lab.network_experiment import decode_handoff
from handoff_lab.protocol import Envelope, ProtocolError, Ref, canonical, nbytes
from handoff_lab.recovery_experiment import RecentReprobe, RecoverySpec, generate


class CodecTests(unittest.TestCase):
    def artifacts(self):
        first = Artifact.make(1, {"α": "雪" * 64, "keep": "quoted \"line\"\n", "remove": [1, None]})
        second = Artifact.make(2, {"α": "雪" * 63 + "雨", "keep": "quoted \"line\"\n", "new": {"x": True}})
        return first, second

    def test_canonical_envelopes_and_quotes_match_original_for_unicode_and_indices(self):
        first, second = self.artifacts()
        codec = PacketCodec()
        for index in (0, 9, 10, 99, 100):
            for current, known in ((first, None), (first, first), (second, first), (second, second)):
                plan = codec.quote(current, known, index, compare_cache=True, recovery_sizes=True)
                full = codec.envelope(current, known, index, "inline")
                self.assertEqual(full, _message(current, index, True))
                self.assertEqual(nbytes(full), plan.inline_bytes)
                target, value, _ = decode_handoff(full, index, None)
                self.assertEqual((target, value.value), (current.ref, current.value))
                if plan.cache_kind:
                    wire = codec.envelope(current, known, index, plan.cache_kind)
                    if plan.cache_kind == "reference":
                        expected = _message(current, index, False)
                    else:
                        expected = Envelope("sweep", "update", "producer", "consumer",
                            {"event": index, "transfer": json.loads(codec.delta(known, current))}).encode()
                    self.assertEqual(wire, expected)
                    self.assertEqual(nbytes(wire), plan.cache_bytes)
                    _, value, action = decode_handoff(wire, index, known)
                    self.assertEqual(value.value, current.value)
                    self.assertEqual(action, plan.cache_kind)
                self.assertEqual(codec.part(current, "snapshot")[0], current.snapshot)
                self.assertEqual(codec.part(current, "ack")[1], plan.ack_bytes)

    def test_lazy_content_is_charged_only_once_and_inline_never_builds_delta(self):
        first, second = self.artifacts()
        codec = PacketCodec()
        a = codec.quote(first, None, 9, compare_cache=False, recovery_sizes=False)
        codec.envelope(first, None, 9, "inline")
        b = codec.quote(first, first, 10, compare_cache=False, recovery_sizes=False)
        self.assertEqual(b.inline_bytes, a.inline_bytes + 1)
        self.assertEqual(codec.materializations["value"], 1)
        self.assertEqual(codec.materializations["inline_attachment"], 1)
        self.assertEqual(codec.materializations["delta"], 0)
        self.assertEqual(codec.materializations["reference_attachment"], 0)
        self.assertEqual(codec.materializations["snapshot"], 0)
        codec.quote(second, first, 11, compare_cache=True, recovery_sizes=True)
        codec.quote(second, first, 12, compare_cache=True, recovery_sizes=True)
        self.assertEqual(codec.materializations["delta"], 1)
        self.assertEqual(codec.materializations["value"], 2)
        self.assertEqual(codec.materializations["snapshot"], 1)

    def test_missing_delta_base_recovers_only_from_decoded_snapshot(self):
        first, second = self.artifacts()
        codec = PacketCodec()
        wire = codec.envelope(second, first, 5, "delta")
        ref, value, kind = decode_handoff(wire, 5, None)
        self.assertEqual((ref, value, kind), (second.ref, None, "delta"))
        recovered = _decode_transfer(codec.recovery(second, None), None)
        self.assertEqual(recovered.value, second.value)
        with self.assertRaises(ProtocolError):
            _decode_transfer(codec.delta(first, second), None)
        malformed = json.loads(codec.recovery(second, None))
        malformed["value"]["new"] = "corrupted"
        with self.assertRaises(ProtocolError):
            _decode_transfer(canonical(malformed), None)
        with self.assertRaises(ProtocolError):
            decode_handoff("not-json", 5, first)

    def test_invalid_source_and_nonadjacent_or_cross_artifact_delta(self):
        first, second = self.artifacts()
        codec = PacketCodec()
        bad = Artifact(first.ref, {"tampered": True})
        with self.assertRaises(ProtocolError):
            codec.quote(bad, None, 0, compare_cache=False, recovery_sizes=False)
        different = Artifact(Ref("different", 2, second.ref.sha256), second.value)
        self.assertIsNone(PacketCodec().delta(first, different))
        third = Artifact.make(3, second.value)
        self.assertIsNone(PacketCodec().delta(first, third))


class EstimatorTests(unittest.TestCase):
    def observation(self, kib, seconds, replies=1):
        return Observation("reference", kib * 1024, replies, replies == 2, seconds)

    def test_empty_and_rank_one_have_explicit_a_only_fallback(self):
        fit = TimeFit(0.9)
        self.assertEqual(fit.fit(), (0, 0, "empty"))
        fit.update(self.observation(1, .003))
        self.assertEqual(fit.fit(), (.003, 0, "underidentified_a_only"))
        fit.update(self.observation(2, .006, 2))
        a, b, label = fit.fit()
        self.assertAlmostEqual(a, .003)
        self.assertEqual((b, label), (0, "underidentified_a_only"))

    def test_nonnegative_interior_fit_recovers_known_coefficients(self):
        fit = TimeFit(0.9)
        for replies, kib in ((1, 1), (1, 8), (2, 4)):
            fit.update(self.observation(kib, .002 * replies + .004 * kib, replies))
        a, b, label = fit.fit()
        self.assertEqual(label, "interior")
        self.assertAlmostEqual(a, .002, places=12)
        self.assertAlmostEqual(b, .004, places=12)

    def test_active_constraints_choose_a_only_or_b_only(self):
        for times, expected in (((3, 2), (2.5, 0, "a_only")), ((1, 3), (0, 1.4, "b_only"))):
            fit = TimeFit(1.0)
            fit.update(self.observation(1, times[0]))
            fit.update(self.observation(2, times[1]))
            a, b, label = fit.fit()
            self.assertAlmostEqual(a, expected[0])
            self.assertAlmostEqual(b, expected[1])
            self.assertEqual(label, expected[2])

    def test_near_singular_fit_uses_declared_fallback(self):
        fit = TimeFit(1.0)
        fit.update(Observation("reference", 2_000_000, 1, False, .1))
        fit.update(Observation("reference", 2_000_001, 1, False, .1000001))
        self.assertEqual(fit.fit()[2], "underidentified_a_only")

    def test_nonfinite_invalid_or_inconsistent_observations_fail(self):
        for seconds in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ProtocolError):
                self.observation(1, seconds)
        with self.assertRaises(ProtocolError):
            Observation("inline", 1024, 2, True, .1)
        with self.assertRaises(ProtocolError):
            Observation("reference", 1024, 1, True, .1)
        fit = TimeFit(.5)
        with self.assertRaises(ProtocolError):
            fit.update(self.observation(1, 1e308))
        self.assertEqual(fit.observations, 0)


class ControllerTests(unittest.TestCase):
    def plan(self, **changes):
        values = dict(index=1, inline_bytes=1000, cache_kind="reference", cache_bytes=100,
                      ack_bytes=100, get_bytes=100, snapshot_bytes=1100)
        values.update(changes)
        return PacketPlan(**values)

    def test_zero_loss_fast_path_does_not_solve_or_need_recovery_lengths(self):
        policy = Controller(MODELS[0])
        with patch.object(policy.model, "fit", side_effect=AssertionError("unneeded fit")):
            decision = policy.choose(self.plan(get_bytes=None, snapshot_bytes=None))
        self.assertEqual(decision.action, "reference")
        self.assertIsNone(decision.a_seconds_per_reply)
        self.assertFalse(policy.needs_recovery_sizes)

    def test_expected_cost_counts_both_miss_frames_and_replies(self):
        policy = Controller(MODELS[0])
        policy.history.append(1)
        with patch.object(policy.model, "fit", return_value=(.005, .001, "test")):
            decision = policy.choose(self.plan())
        self.assertEqual(decision.action, "inline")
        self.assertAlmostEqual(decision.inline_score_seconds, .005 + .001 * 1108 / 1024)
        self.assertAlmostEqual(decision.cache_score_seconds, .010 + .001 * (208 + 1208) / 1024)
        self.assertFalse(decision.forced_probe)

    def test_bounded_probe_is_an_ordinary_delivery_and_inline_does_not_clear_loss(self):
        policy = Controller(MODELS[0])  # lambda .5, K2
        policy.observe(Observation("reference", 1500, 2, True, .020))
        with patch.object(policy.model, "fit", return_value=(.005, .001, "test")):
            self.assertEqual(policy.choose(self.plan()).action, "inline")
            policy.observe(Observation("inline", 1108, 1, False, .010))
            self.assertEqual(list(policy.history), [1])
            decision = policy.choose(self.plan(index=2))
        self.assertEqual(decision.action, "reference")
        self.assertTrue(decision.forced_probe)
        policy.observe(Observation("reference", 208, 1, False, .006))
        self.assertEqual(list(policy.history), [1, 0])

    def test_miss_guard_waits_only_eligible_opportunities_and_restores_on_success(self):
        guard = Controller(GUARDS[0])
        self.assertEqual(guard.choose(self.plan()).action, "reference")
        guard.observe(Observation("reference", 1500, 2, True, .020))
        self.assertEqual(guard.choose(self.plan()).action, "inline")
        self.assertEqual(guard.suppressed, 1)
        self.assertEqual(guard.choose(self.plan(cache_kind=None, cache_bytes=None)).action, "inline")
        self.assertEqual(guard.suppressed, 1)
        probe = guard.choose(self.plan())
        self.assertEqual((probe.action, probe.forced_probe), ("reference", True))
        guard.observe(Observation("reference", 208, 1, False, .006))
        self.assertTrue(guard.trusted)
        self.assertFalse(guard.choose(self.plan()).forced_probe)

    def test_recent_rule_matches_frozen_implementation_across_updates_and_losses(self):
        for size, changed in ((128, 1), (2048, 1), (4096, 8), (4096, 16)):
            workload = generate(RecoverySpec(size, 30, 3, changed, "delta_sync", 140009,
                                             "tuning", "healthy"))
            original, optimized, codec, known = RecentReprobe(4, 4), Controller(RECENT), PacketCodec(), None
            for index, current in enumerate(workload.artifacts):
                view = View("delta_sync", "direct", known, None)
                old, forced, eligible = original.choose(current, index, view)
                plan = codec.quote(current, known, index, compare_cache=True,
                                   recovery_sizes=optimized.needs_recovery_sizes)
                new = optimized.choose(plan)
                self.assertEqual((new.action, new.forced_probe, new.eligible), (old.kind, forced, eligible))
                actual = None if index in {1, 2, 3, 10, 11, 13, 20, 21, 22} else known
                ledger = account(old, current, view, actual, validate=True)
                original.observe(current, view, old, ledger)
                optimized.observe(Observation(new.action, ledger.total_bytes + 8 + 8 * ledger.cache_misses,
                    1 + ledger.cache_misses, bool(ledger.cache_misses), .001))
                self.assertEqual(list(optimized.history), list(original.window))
                self.assertEqual(optimized.suppressed, original.suppressed)
                known = current

    def test_time_policy_interface_has_no_profile_or_receiver_input(self):
        self.assertEqual(list(inspect.signature(Controller.choose).parameters), ["self", "plan"])
        self.assertEqual(list(inspect.signature(Controller.observe).parameters), ["self", "observation"])
        self.assertEqual(set(PacketPlan.__dataclass_fields__), {"index", "inline_bytes", "cache_kind",
            "cache_bytes", "ack_bytes", "get_bytes", "snapshot_bytes"})


class SocketTests(unittest.TestCase):
    def case(self, **changes):
        values = dict(split="heldout", payload_chars=640, seed=140021,
                      schedule="short_single", handoffs=7, profile=UNSHAPED)
        values.update(changes)
        return Case(**values)

    def test_tiny_actual_tcp_sessions_are_exact_and_replayable_for_every_candidate(self):
        for policy in TUNING_POLICIES:
            row = run_case(self.case(), policy)
            self.assertTrue(row["completed"], row["error"])
            self.assertTrue(row["receiver_values_exact"])
            self.assertTrue(row["policy_replay_exact"])
            self.assertEqual(row["acknowledgments"], 7)
            self.assertEqual(row["frames"], 1 + 2*7 + 2*row["recovery_requests"])
            self.assertEqual(row["framed_stream_bytes"], row["payload_bytes"] + 4*row["frames"])
            sender, receiver = row["sender_transport"], row["receiver_transport"]
            self.assertEqual(sender["sent_stream_sha256"], receiver["received_stream_sha256"])
            self.assertEqual(sender["received_stream_sha256"], receiver["sent_stream_sha256"])
            self.assertEqual(row["receiver_executed_loss_events"], [False, False, True, False, False, False, False])

    def test_pacing_and_reply_holdoff_are_real_and_all_charged(self):
        row = run_case(self.case(handoffs=3, profile=Profile("functional_only", .001, 1_000_000)), CACHE)
        self.assertTrue(row["completed"], row["error"])
        self.assertEqual(row["recovery_requests"], 1)
        server = row["receiver_transport"]
        self.assertAlmostEqual(server["requested_holdoff_seconds"], .004)
        pacing = sum(row[name]["requested_pacing_seconds"] for name in ("sender_transport", "receiver_transport"))
        self.assertAlmostEqual(pacing, row["framed_stream_bytes"] / 1_000_000)
        self.assertGreaterEqual(row["completion_seconds"], .004 + pacing)

    def test_fresh_source_objects_prevent_cross_run_memoization(self):
        a, b = prepare_artifacts(self.case()), prepare_artifacts(self.case())
        self.assertEqual(a, b)
        self.assertIsNot(a[0], b[0])
        _ = a[0].snapshot
        self.assertNotIn("snapshot", b[0].__dict__)

    def test_setup_and_thread_start_failures_are_retained(self):
        for target in ("handoff_lab.latency_experiment.socket.socket",
                       "handoff_lab.latency_experiment.threading.Thread.start"):
            with patch(target, side_effect=OSError("functional setup failure")):
                row = run_attempt(self.case(handoffs=2), CACHE)
            self.assertFalse(row["completed"])
            self.assertEqual(row["failure_stage"], "connection_setup")
            self.assertIn("functional setup failure", row["error"])
            self.assertIsNone(row["framed_stream_bytes"])

    def test_receiver_rejects_corrupt_actual_wire_and_partial_counters_survive(self):
        original = PacketCodec.envelope
        def corrupt(codec, current, known, index, action):
            return "x" * nbytes(original(codec, current, known, index, action))
        with patch.object(PacketCodec, "envelope", corrupt):
            row = run_attempt(self.case(handoffs=2), CACHE)
        self.assertFalse(row["completed"])
        self.assertIn("ProtocolError", row["receiver_error"])
        self.assertEqual(row["data_frames"], 1)
        self.assertEqual(row["acknowledgments"], 0)
        self.assertGreater(row["framed_stream_bytes"], 0)
        self.assertEqual(row["partial_event"]["index"], 0)
        self.assertEqual(row["sender_transport"]["sent_stream_bytes"], row["receiver_transport"]["received_stream_bytes"])

    def test_post_audit_exception_keeps_the_completed_transfer_counters(self):
        with patch("handoff_lab.latency_experiment.replay_decisions", side_effect=ValueError("audit fixture")):
            row = run_attempt(self.case(handoffs=2), CACHE)
        self.assertFalse(row["completed"])
        self.assertEqual(row["failure_stage"], "post_run_audit_exception")
        self.assertEqual(row["data_frames"], 2)
        self.assertEqual(row["acknowledgments"], 2)
        self.assertEqual(len(row["handoff_records"]), 2)
        self.assertGreater(row["framed_stream_bytes"], 0)
        self.assertIsNotNone(row["receiver_transport"])

    def test_replay_never_reads_hidden_schedule_and_checks_recorded_decisions(self):
        row = run_case(self.case(), MODELS[0])
        self.assertTrue(row["completed"], row["error"])
        copied = copy.deepcopy(row)
        copied["case"] = {"opaque": "not available to replay"}
        copied["receiver_executed_loss_events"] = [True] * 100
        self.assertTrue(replay_decisions(copied))
        copied["handoff_records"] = copied["handoff_records"][:3]
        self.assertTrue(replay_decisions(copied))
        copied["handoff_records"][0]["decision"]["action"] = "reference"
        self.assertFalse(replay_decisions(copied))


def synthetic_rows(split, selection=None, factors=None):
    """Invented test-only numbers for aggregation logic; never experiment outputs."""
    rows = []
    factors = factors or {}
    for ordinal, (rep, case, policy) in enumerate(execution_order(split, selection)):
        row = _run_record(case, policy)
        row.update({"repetition": rep, "execution_ordinal": ordinal, "completed": True,
                    "receiver_values_exact": True, "policy_replay_exact": True,
                    "completion_seconds": factors.get(policy.name, 1.0) * (1 + rep*.01),
                    "framed_stream_bytes": 100, "payload_bytes": 80, "frames": 5,
                    "application_framing_bytes": 20})
        rows.append(row)
    return rows


class DesignAndReportingTests(unittest.TestCase):
    def selection(self):
        return {"miss_guard": GUARDS[0].to_dict(), "model": MODELS[0].to_dict(), "simple_baseline": CACHE.name}

    def test_fixed_grids_order_and_prefix_rules(self):
        selection = self.selection()
        for split, count, nc in (("tuning", 324, 12), ("heldout", 360, 24)):
            order = execution_order(split, selection)
            self.assertEqual(len(order), count)
            self.assertEqual(len(cases(split)), nc)
            self.assertEqual(order, execution_order(split, selection))
            self.assertEqual(len({(rep, case.key(), spec.name) for rep, case, spec in order}), count)
            for case in cases(split):
                rng = random.Random(case.seed ^ 0x1A2B3C4D)
                long = [lose_before(split, case.schedule, index, rng.random()) for index in range(40)]
                rng = random.Random(case.seed ^ 0x1A2B3C4D)
                short = [lose_before(split, case.schedule, index, rng.random()) for index in range(5)]
                self.assertEqual(long[:5], short)
                a = prepare_artifacts(case)
                b = prepare_artifacts(Case(**{**case.__dict__, "handoffs": 5}))
                self.assertEqual(a[:5], b)
        self.assertEqual(sum(case.handoffs for _, case, _ in execution_order("heldout", selection)), 6570)

    def test_selection_uses_global_complete_tuning_scores_and_fixed_ties(self):
        rows = synthetic_rows("tuning")
        result = tuning_selection(rows)
        self.assertIsNone(result["error"])
        self.assertEqual(result["selection"]["miss_guard"], GUARDS[0].to_dict())
        self.assertEqual(result["selection"]["model"], PolicySpec("latency_model", 2, .9).to_dict())
        self.assertEqual(result["selection"]["simple_baseline"], CACHE.name)
        factors = {GUARDS[1].name: .7, MODELS[1].name: .6, INLINE.name: .8}
        result = tuning_selection(synthetic_rows("tuning", factors=factors))
        self.assertEqual(result["selection"]["miss_guard"], GUARDS[1].to_dict())
        self.assertEqual(result["selection"]["model"], MODELS[1].to_dict())
        self.assertEqual(result["selection"]["simple_baseline"], GUARDS[1].name)

    def test_failed_candidate_is_ineligible_and_missing_cache_prevents_selection(self):
        rows = synthetic_rows("tuning", factors={MODELS[0].name: .5})
        next(row for row in rows if row["policy"] == MODELS[0].name)["completed"] = False
        result = tuning_selection(rows)
        self.assertIsNone(result["scores"][MODELS[0].name])
        self.assertNotEqual(result["selection"]["model"], MODELS[0].to_dict())
        next(row for row in rows if row["policy"] == CACHE.name)["completed"] = False
        self.assertIsNone(tuning_selection(rows)["selection"])

    def test_paired_median_is_distinct_from_ratio_of_medians(self):
        case = cases("tuning")[0]
        rows = []
        for rep, (a, b) in enumerate(zip((2, 3, 100), (1, 100, 4))):
            for policy, duration in ((INLINE, a), (CACHE, b)):
                row = _run_record(case, policy)
                row.update(repetition=rep, completed=True, completion_seconds=duration)
                rows.append(row)
        result = paired_comparison(rows, case, INLINE.name, CACHE.name)
        self.assertEqual(result["median_paired_ratio"], 2)
        self.assertNotEqual(result["median_paired_ratio"], 3/4)

    def test_strengthened_guard_gate_prevents_miscrediting_time_fit(self):
        selection = self.selection()
        factors = {MODELS[0].name: .9, GUARDS[0].name: .8, INLINE.name: 1.1}
        report = heldout_analysis(synthetic_rows("heldout", selection, factors), selection)
        self.assertTrue(report["gates"]["five_percent_vs_validation_selected_simple"])
        self.assertFalse(report["simple_parity_checks"][GUARDS[0].name])
        self.assertFalse(report["all_engineering_gates_met"])
        rows = synthetic_rows("heldout", selection, {MODELS[0].name: .8, GUARDS[0].name: .9})
        self.assertTrue(heldout_analysis(rows, selection)["all_engineering_gates_met"])
        rows[0]["completed"] = False
        self.assertFalse(heldout_analysis(rows, selection)["all_engineering_gates_met"])

    def test_budget_or_warmup_failures_retain_every_unstarted_cell(self):
        def fake(case, policy):
            row = _run_record(case, policy)
            row["completed"] = True
            return row
        with patch("handoff_lab.latency_experiment.run_attempt", side_effect=fake) as runner:
            with patch("handoff_lab.latency_experiment.BUDGET_SECONDS", 0):
                report = run_batch("tuning", source_commit="1" * 40)
        self.assertEqual(runner.call_count, len(TUNING_POLICIES))  # Only mocked functional warmups.
        self.assertEqual(len(report["runs"]), 324)
        self.assertEqual(report["missing_runs"], 324)
        self.assertIsNone(report["selection"])
        self.assertEqual({row["failure_stage"] for row in report["runs"]}, {"not_started"})

    def test_complete_packaging_selection_recomputation_and_hash_gate(self):
        rows = synthetic_rows("tuning")
        tuning = tuning_selection(rows)
        report = {"experiment": "causal_completion_time_v1", "split": "tuning",
                  "source_commit": "1" * 40, "design_commit": DESIGN_COMMIT,
                  "source_hashes": source_hashes(), "tuning": tuning,
                  "selection": tuning["selection"], "runs": rows, "warmups": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            summary = write_report(report, path)
            packed = path.with_name(summary["full_report"]["file"]).read_bytes()
            self.assertEqual(json.loads(gzip.decompress(packed)), report)
            self.assertEqual(hashlib.sha256(packed).hexdigest(), summary["full_report"]["sha256"])
            self.assertNotIn("runs", summary)
            self.assertEqual(load_selection(path), (tuning["selection"], "1" * 40))
            with patch("handoff_lab.latency_experiment.source_hashes", return_value={"changed": "source"}):
                with self.assertRaisesRegex(ValueError, "provenance changed"):
                    load_selection(path)
            summary["selection"]["simple_baseline"] = INLINE.name
            path.write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, "does not reproduce"):
                load_selection(path)


if __name__ == "__main__":
    unittest.main()
