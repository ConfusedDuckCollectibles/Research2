import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from handoff_lab.adaptive_transport import Artifact, View
from handoff_lab.protocol import ProtocolError, canonical
from handoff_lab.recovery_experiment import (CANDIDATES, POLICIES, RecoverySpec,
    RecoveryWorkload, decode_status_reply, fingerprints, generate, phase_at, run,
    specs, status_exchange, validate_selection, write_report)


class RecoveryExperimentTests(unittest.TestCase):
    def workload(self, **kwargs):
        # Dedicated unit-test data; these are not the experiment's outcome grids.
        params = dict(payload_chars=1024, uses=32, update_every=0, changed_fields=0,
                      topology="delta_sync", seed=991, split="stress", schedule="early_single_loss")
        params.update(kwargs)
        return generate(RecoverySpec(**params))

    def test_frozen_grid_counts_and_new_seed_sets(self):
        self.assertEqual(len(specs("tuning")), 192)
        self.assertEqual(len(specs("heldout")), 720)
        self.assertEqual(len(specs("stress")), 16)
        self.assertEqual({s.seed for s in specs("tuning")}, {57103, 57119})
        self.assertEqual({s.seed for s in specs("heldout")}, {68311, 68329})
        self.assertEqual({s.seed for s in specs("stress")}, {79433})

    def test_absolute_phase_boundaries(self):
        self.assertEqual(phase_at("tuning", "loss_recovery", 7)[0], 1)
        self.assertEqual(phase_at("tuning", "loss_recovery", 8)[0], 0)
        self.assertEqual(phase_at("heldout", "healthy_loss_recovery", 14)[0], 1)
        self.assertEqual(phase_at("heldout", "healthy_loss_recovery", 15)[1], "recovery")

    def test_generation_prefix_and_separate_random_streams(self):
        long = self.workload(uses=40, update_every=3, changed_fields=1,
                             split="tuning", schedule="healthy_loss_recovery")
        short = self.workload(uses=11, update_every=3, changed_fields=1,
                              split="tuning", schedule="healthy_loss_recovery")
        self.assertEqual(long.artifacts[:11], short.artifacts)
        self.assertEqual(long.losses[:11], short.losses)
        self.assertEqual(long.phases[:11], short.phases)
        healthy = self.workload(uses=40, update_every=3, changed_fields=1,
                                split="tuning", schedule="healthy")
        self.assertEqual(long.artifacts, healthy.artifacts)
        self.assertNotEqual(long.losses, healthy.losses)

    def test_all_candidates_have_exact_healthy_byte_parity(self):
        for topology in ("revision_cache", "delta_sync"):
            workload = self.workload(uses=20, update_every=3, changed_fields=1,
                                     topology=topology, split="tuning", schedule="healthy")
            baseline = run(workload, "cache_delta")
            for window, interval in CANDIDATES:
                result = run(workload, "recent_reprobe", window=window, interval=interval)
                self.assertEqual(result["total_bytes"], baseline["total_bytes"])
                self.assertEqual(result["forced_reprobes"], 0)

    def test_bounded_reprobing_recovers_from_prior_trap(self):
        workload = self.workload()
        old = run(workload, "original_online")
        improved = run(workload, "recent_reprobe", window=4, interval=2)
        self.assertTrue(old["recovery_segments"]["recovery"]["censored"])
        self.assertFalse(improved["recovery_segments"]["recovery"]["censored"])
        self.assertGreater(improved["forced_reprobes"], 0)

    def test_failed_reprobes_have_real_recovery_cost(self):
        workload = self.workload(uses=14, split="tuning", schedule="permanent_loss")
        result = run(workload, "recent_reprobe", window=4, interval=2, trace=True)
        forced = [row for row in result["trace"] if row["forced_reprobe"]]
        self.assertTrue(forced)
        for row in forced:
            self.assertIn(row["action"], {"reference", "delta"})
            self.assertEqual(row["cache_misses"], 1)
            self.assertGreater(row["recovery_request_bytes"], 0)
            self.assertGreater(row["recovery_response_bytes"], 0)
            self.assertGreater(row["receiver_ack_bytes"], 0)

    def test_online_does_not_know_current_hidden_loss(self):
        healthy = self.workload(uses=2, split="tuning", schedule="healthy")
        lost = RecoveryWorkload(healthy.spec, healthy.artifacts, (False, True), healthy.phases)
        for policy in ("original_online", "recent_reprobe"):
            a = run(healthy, policy, trace=True)["trace"]
            b = run(lost, policy, trace=True)["trace"]
            self.assertEqual([row["action"] for row in a], [row["action"] for row in b])
            self.assertEqual(a[1]["cache_misses"], 0)
            self.assertEqual(b[1]["cache_misses"], 1)

    def test_feedback_changes_action_only_after_paid_exchange(self):
        workload = self.workload(uses=2)
        ordinary = run(workload, "cache_delta", trace=True)
        feedback = run(workload, "status_feedback", trace=True)
        self.assertEqual(ordinary["trace"][1]["action"], "reference")
        self.assertEqual(feedback["trace"][1]["action"], "inline")
        self.assertEqual(feedback["status_queries"], 1)
        self.assertGreater(feedback["status_request_bytes"], 0)
        self.assertGreater(feedback["status_response_bytes"], 0)

    def test_status_reply_validation_and_knowledge_boundary(self):
        current = Artifact.make(1, {"x": 1})
        view = View("delta_sync", "direct", current, None)
        observed, request_bytes, reply_bytes = status_exchange(current, view, None)
        self.assertIsNone(observed.known_cache)
        self.assertGreater(request_bytes + reply_bytes, 0)
        wrong = Artifact.make(2, {"x": 2})
        for reply in ("not-json", '{"kind":"wrong","have":null}',
                      canonical({"kind": "cache_status_reply", "have": wrong.ref.to_dict()})):
            with self.assertRaises(ProtocolError):
                decode_status_reply(reply, view)

    def test_every_policy_is_prefix_invariant(self):
        long = self.workload(uses=28, update_every=3, changed_fields=1,
                             split="tuning", schedule="healthy_loss_recovery")
        short = self.workload(uses=13, update_every=3, changed_fields=1,
                              split="tuning", schedule="healthy_loss_recovery")
        for policy in POLICIES:
            a = run(long, policy, trace=True)["trace"][:13]
            b = run(short, policy, trace=True)["trace"]
            self.assertEqual(a, b)

    def test_all_accounting_components_and_round_trips(self):
        workload = self.workload(uses=12)
        for policy in POLICIES:
            result = run(workload, policy, trace=True)
            components = [value for key, value in result.items() if key.endswith("_bytes") and key != "total_bytes"]
            self.assertEqual(sum(components), result["total_bytes"])
            self.assertEqual(sum(row["total_bytes"] for row in result["trace"]) + result["capability_setup_bytes"],
                             result["total_bytes"])
            self.assertEqual(sum(row["bytes"] for row in result["phases"].values()) + result["capability_setup_bytes"],
                             result["total_bytes"])
            self.assertEqual(result["logical_round_trips"],
                             workload.spec.uses + result["cache_misses"] + result["status_queries"])
            self.assertTrue(result["all_reconstructions_exact"])

    def test_selection_rejects_changed_code_design_or_parameters(self):
        selection = {"experiment": "cache_recovery_v1_selection", "selection_success": True,
                     "heldout_run": False, "selected_parameters": {"window": 4, "interval": 2},
                     "selected_fixed_by_topology": {"revision_cache": "inline", "delta_sync": "cache_delta"},
                     **fingerprints()}
        validate_selection(selection)
        for key, bad in (("design_sha256", "bad"), ("implementation_sha256", {}),
                         ("selected_parameters", {"window": 128, "interval": 1}),
                         ("selected_fixed_by_topology", {"delta_sync": "original_online"})):
            changed = copy.deepcopy(selection)
            changed[key] = bad
            with self.assertRaises(ValueError):
                validate_selection(changed)

    def test_summary_compression_preserves_full_rows(self):
        report = {"experiment": "test", "tuning_rows": [{"id": 1}], "candidate_scores": {"a": 4}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            summary = write_report(report, path)
            self.assertNotIn("tuning_rows", summary)
            full = path.with_name(summary["full_report"]["file"])
            self.assertEqual(json.loads(gzip.decompress(full.read_bytes())), report)


if __name__ == "__main__":
    unittest.main()
