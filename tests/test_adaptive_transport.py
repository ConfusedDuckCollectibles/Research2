import json
import gzip
import tempfile
import unittest
from pathlib import Path

from handoff_lab.adaptive_transport import (Artifact, FIXED_POLICIES, Spec, Workload,
    Transmission, View, _decode_transfer, _delta, _message, account, generate, grid,
    run, switch_oracle, write_report)
from handoff_lab.protocol import ProtocolError


class AdaptiveTransportTests(unittest.TestCase):
    def workload(self, **kwargs):
        values = dict(payload_chars=4096, uses=16, update_every=0, changed_fields=0,
                      topology="delta_sync", deployment="direct", seed=7)
        values.update(kwargs)
        return generate(Spec(**values))

    def test_direct_cache_baseline_requires_no_external_store(self):
        workload = self.workload()
        baseline = run(workload, "cache_delta")
        self.assertEqual(baseline["actions"], {"inline": 1, "reference": 15, "delta": 0})
        self.assertEqual(baseline["store_write_bytes"], 0)
        self.assertEqual(baseline["recovery_response_bytes"], 0)
        self.assertLess(baseline["total_bytes"], run(workload, "inline")["total_bytes"])
        self.assertEqual(run(workload, "online")["total_bytes"], baseline["total_bytes"])

    def test_small_delta_beats_repeated_snapshots(self):
        workload = self.workload(update_every=1, changed_fields=1)
        delta = run(workload, "cache_delta")
        snapshot = run(workload, "cache_snapshot")
        self.assertEqual(delta["actions"]["delta"], 15)
        self.assertLess(delta["total_bytes"], snapshot["total_bytes"])

    def test_large_changes_fall_back_to_inline(self):
        workload = self.workload(update_every=1, changed_fields=16)
        self.assertEqual(run(workload, "cache_delta")["actions"]["inline"], 16)

    def test_no_cache_direct_baseline_is_already_inline(self):
        workload = self.workload(topology="no_cache")
        inline = run(workload, "inline")
        for policy in (*FIXED_POLICIES, "online"):
            result = run(workload, policy)
            self.assertEqual(result["total_bytes"], inline["total_bytes"])
            self.assertEqual(result["cache_misses"], 0)

    def test_store_setup_ack_fetch_and_reply_are_charged(self):
        workload = self.workload(uses=1, deployment="external_store")
        result = run(workload, "cache_delta")
        for field in ("store_write_bytes", "store_ack_bytes", "recovery_request_bytes",
                      "recovery_response_bytes", "receiver_ack_bytes", "capability_setup_bytes"):
            self.assertGreater(result[field], 0)
        components = [value for key, value in result.items() if key.endswith("_bytes") and key != "total_bytes"]
        self.assertEqual(sum(components), result["total_bytes"])

    def test_inline_seeds_same_cache_before_admission(self):
        workload = self.workload(deployment="external_store")
        result = run(workload, "inline_once_cache", trace=True)
        self.assertEqual(result["trace"][0]["action"], "inline")
        self.assertTrue(result["trace"][1]["published"])
        self.assertEqual(result["trace"][1]["recovery_response_bytes"], 0)

    def test_unannounced_loss_recovers_and_is_fully_charged(self):
        workload = self.workload(loss_probability=1.0)
        result = run(workload, "cache_delta", trace=True)
        self.assertEqual(result["cache_misses"], 15)
        self.assertTrue(result["all_reconstructions_exact"])
        self.assertGreater(result["recovery_request_bytes"], 0)
        self.assertGreater(result["recovery_response_bytes"], 0)
        self.assertGreater(result["total_bytes"], run(workload, "inline")["total_bytes"])

    def test_online_does_not_see_current_hidden_loss(self):
        healthy = self.workload(uses=2)
        lost = Workload(healthy.spec, healthy.artifacts, (False, True))
        a = run(healthy, "online", trace=True)["trace"]
        b = run(lost, "online", trace=True)["trace"]
        self.assertEqual([row["action"] for row in a], [row["action"] for row in b])
        self.assertEqual(a[1]["cache_misses"], 0)
        self.assertEqual(b[1]["cache_misses"], 1)

    def test_online_decisions_do_not_depend_on_future_length(self):
        long = self.workload(deployment="external_store", update_every=4, changed_fields=1)
        short_spec = Spec(**{**long.spec.to_dict(), "uses": 7})
        short = Workload(short_spec, long.artifacts[:7], long.losses[:7])
        a = run(long, "online", threshold=2, trace=True)["trace"][:7]
        b = run(short, "online", threshold=2, trace=True)["trace"]
        self.assertEqual(a, b)

    def test_delta_integrity_and_stale_base_are_checked(self):
        a = Artifact.make(1, {"one": 1, "two": 2})
        b = Artifact.make(2, {"one": 3})
        packet = _delta(a, b)
        self.assertEqual(_decode_transfer(packet, a).value, b.value)
        with self.assertRaises(ProtocolError):
            _decode_transfer(packet, None)
        bad = json.loads(packet)
        bad["result"]["sha256"] = "0" * 64
        with self.assertRaises(ProtocolError):
            _decode_transfer(json.dumps(bad), a)

    def test_oracle_is_explicitly_future_aware_and_includes_never_buy(self):
        workload = self.workload(uses=1, deployment="external_store")
        oracle = switch_oracle(workload)
        self.assertEqual(oracle["switch_at"], 1)
        self.assertEqual(oracle["total_bytes"], run(workload, "inline")["total_bytes"])
        self.assertIn("future", oracle["scope"])

    def test_splits_use_disjoint_sizes_and_no_duplicate_specs(self):
        tuning, heldout = grid("tuning"), grid("heldout")
        self.assertFalse({s.payload_chars for s in tuning} & {s.payload_chars for s in heldout})
        self.assertEqual(len(tuning), len(set(tuning)))
        self.assertEqual(len(heldout), len(set(heldout)))

    def test_reference_validation_reads_actual_wire(self):
        current = Artifact.make(1, {"x": 1})
        view = View("delta_sync", "direct", current, None)
        with self.assertRaises(ProtocolError):
            account(Transmission("reference", "not-json", None, None, True),
                    current, view, current, validate=True)
        wrong = Artifact.make(2, {"x": 2})
        with self.assertRaises(ProtocolError):
            account(Transmission("reference", _message(wrong, 0, False), None, None, True),
                    current, view, current, validate=True)

    def test_delta_validation_cannot_use_out_of_band_packet(self):
        old = Artifact.make(1, {"x": 1})
        current = Artifact.make(2, {"x": 2})
        view = View("delta_sync", "direct", old, None)
        with self.assertRaises(ProtocolError):
            account(Transmission("delta", "not-json", None, _delta(old, current), True),
                    current, view, old, validate=True)

    def test_external_recovery_requires_published_content(self):
        current = Artifact.make(1, {"x": 1})
        view = View("delta_sync", "external_store", None, None)
        with self.assertRaises(ProtocolError):
            account(Transmission("reference", _message(current, 0, False), None, None, True),
                    current, view, None, validate=True)

    def test_summary_and_compressed_full_report_preserve_all_observations(self):
        report = {"schema_version": 1, "heldout_summary": {"total": 123},
                  "heldout_rows": [{"id": 1}], "stress": [{"id": 2}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            summary = write_report(report, path)
            self.assertNotIn("heldout_rows", summary)
            self.assertNotIn("stress", summary)
            full = path.with_name(summary["full_report"]["file"])
            self.assertEqual(json.loads(gzip.decompress(full.read_bytes())), report)
            first = full.read_bytes()
            write_report(report, path)
            self.assertEqual(full.read_bytes(), first)


if __name__ == "__main__":
    unittest.main()
