import copy
import gzip
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from handoff_lab.adaptive_transport import Artifact
from handoff_lab.network_experiment import (Case, FrameError, FramedSocket, POLICIES,
    PROFILES, Profile, capabilities, cases, decode_handoff, execution_order,
    lose_before, parse_reply, prepare_artifacts, run_attempt, run_case, write_report)
from handoff_lab.protocol import Envelope, MAX_MESSAGE_BYTES, ProtocolError, canonical


class FramingTests(unittest.TestCase):
    def test_partial_prefix_and_payload_reads_reconstruct_actual_unicode(self):
        left, right = socket.socketpair()
        try:
            payload = canonical({"content": "αβγ", "values": [1, 2, 3]})
            encoded = payload.encode()
            raw = struct.pack("!I", len(encoded)) + encoded
            def producer():
                for byte in raw:
                    left.sendall(bytes([byte]))
            worker = threading.Thread(target=producer)
            worker.start()
            stream = FramedSocket(right, PROFILES[0], receiver=True)
            self.assertEqual(stream.recv_frame(), payload)
            worker.join()
            self.assertEqual(stream.snapshot()["received_stream_bytes"], len(raw))
        finally:
            left.close()
            right.close()

    def test_oversize_and_truncated_frames_fail(self):
        for raw in (struct.pack("!I", MAX_MESSAGE_BYTES + 1), struct.pack("!I", 5) + b"ab"):
            left, right = socket.socketpair()
            try:
                left.sendall(raw)
                left.shutdown(socket.SHUT_WR)
                with self.assertRaises(FrameError):
                    FramedSocket(right, PROFILES[0], receiver=True).recv_frame()
            finally:
                left.close()
                right.close()

    def test_pacing_and_holdoff_are_counted_before_received_completion(self):
        left, right = socket.socketpair()
        try:
            profile = Profile("test", 0.001, 1_000_000)
            sender = FramedSocket(left, profile, receiver=True)
            receiver = FramedSocket(right, PROFILES[0], receiver=False)
            payload = canonical({"kind": "ack", "padding": "x" * 1300})
            worker = threading.Thread(target=lambda: sender.send_frame(payload, reply=True))
            worker.start()
            self.assertEqual(receiver.recv_frame(), payload)
            worker.join()
            stats = sender.snapshot()
            expected = len(payload.encode()) + 4
            self.assertEqual(stats["sent_stream_bytes"], expected)
            self.assertAlmostEqual(stats["requested_pacing_seconds"], expected / 1_000_000)
            self.assertEqual(stats["requested_holdoff_seconds"], 0.001)
            self.assertGreaterEqual(stats["observed_holdoff_wait_seconds"], 0.001)
            self.assertGreaterEqual(stats["observed_pacing_wait_seconds"], expected / 1_000_000)
        finally:
            left.close()
            right.close()


class NetworkProtocolTests(unittest.TestCase):
    def case(self, **kwargs):
        values = dict(payload_chars=640, seed=123456, schedule="healthy", profile=PROFILES[0], handoffs=8)
        values.update(kwargs)
        return Case(**values)

    def test_actual_tcp_reconstructs_all_policy_outputs_and_reconciles_streams(self):
        for policy in POLICIES:
            result = run_case(self.case(schedule="single_loss"), policy)
            self.assertTrue(result["completed"], result["error"])
            self.assertTrue(result["receiver_values_exact"])
            self.assertEqual(result["data_frames"], 8)
            self.assertEqual(result["acknowledgments"], 8)
            sender, receiver = result["sender_transport"], result["receiver_transport"]
            self.assertEqual(sender["sent_stream_sha256"], receiver["received_stream_sha256"])
            self.assertEqual(sender["received_stream_sha256"], receiver["sent_stream_sha256"])
            self.assertEqual(result["framed_stream_bytes"], result["payload_bytes"] + result["application_framing_bytes"])
            self.assertEqual(result["application_framing_bytes"], 4 * result["frames"])
            self.assertEqual(result["frames"], 1 + 2 * 8 + 2 * result["cache_misses"])

    def test_real_deltas_are_used_and_missing_base_recovers(self):
        healthy = run_case(self.case(payload_chars=2048), "cache_delta")
        lost = run_case(self.case(payload_chars=2048, schedule="loss_recovery"), "cache_delta")
        self.assertTrue(healthy["completed"], healthy["error"])
        self.assertTrue(lost["completed"], lost["error"])
        self.assertIn("delta", [row["action"] for row in healthy["handoff_records"]])
        self.assertGreater(lost["recovery_responses"], 0)
        self.assertGreater(lost["payload_bytes"], healthy["payload_bytes"])

    def test_receiver_reply_holdoff_matches_actual_get_and_ack_count(self):
        profile = Profile("test", 0.001, 1_000_000)
        result = run_case(self.case(profile=profile, handoffs=3, schedule="single_loss"), "cache_delta")
        self.assertTrue(result["completed"], result["error"])
        replies = result["acknowledgments"] + result["recovery_requests"]
        stats = result["receiver_transport"]
        self.assertAlmostEqual(stats["requested_holdoff_seconds"], replies * 0.001)
        self.assertEqual(result["sender_transport"]["requested_holdoff_seconds"], 0)
        total_pacing = result["sender_transport"]["requested_pacing_seconds"] + stats["requested_pacing_seconds"]
        self.assertAlmostEqual(total_pacing, result["framed_stream_bytes"] / 1_000_000)

    def test_fresh_artifact_objects_have_no_cross_run_snapshot_cache(self):
        a = prepare_artifacts(self.case())
        b = prepare_artifacts(self.case())
        self.assertEqual(a, b)
        self.assertIsNot(a[0], b[0])
        self.assertNotIn("snapshot", a[0].__dict__)
        self.assertNotIn("snapshot", b[0].__dict__)
        _ = a[0].snapshot
        self.assertIn("snapshot", a[0].__dict__)
        self.assertNotIn("snapshot", b[0].__dict__)

    def test_real_socket_prefix_preserves_policy_actions(self):
        for policy in POLICIES:
            long = run_case(self.case(schedule="single_loss", handoffs=9), policy)
            short = run_case(self.case(schedule="single_loss", handoffs=5), policy)
            self.assertTrue(long["completed"] and short["completed"])
            fields = ("index", "action", "forced_reprobe", "cache_misses", "data_payload_sha256")
            a = [{key: row[key] for key in fields} for row in long["handoff_records"][:5]]
            b = [{key: row[key] for key in fields} for row in short["handoff_records"]]
            self.assertEqual(a, b)

    def test_no_current_hidden_loss_leaks_to_policy_choice(self):
        for policy in ("original_online", "recent_reprobe"):
            healthy = run_case(self.case(handoffs=2), policy)
            lost = run_case(self.case(handoffs=2, schedule="single_loss"), policy)
            self.assertEqual([r["action"] for r in healthy["handoff_records"]],
                             [r["action"] for r in lost["handoff_records"]])
            self.assertEqual(healthy["cache_misses"], 0)
            self.assertEqual(lost["cache_misses"], 1)

    def test_corrupt_wire_and_wrong_ack_are_rejected(self):
        artifact = Artifact.make(1, {"value": 1})
        with self.assertRaises(ProtocolError):
            decode_handoff("not-json", 0, artifact)
        with self.assertRaises(ProtocolError):
            parse_reply(canonical({"kind": "ack", "ref": artifact.ref.to_dict(), "retained": False}), artifact, None)

    def test_fixed_order_is_complete_balanced_and_repeatable(self):
        order = execution_order()
        self.assertEqual(order, execution_order())
        self.assertEqual(len(order), 216)
        self.assertEqual(len(cases()), 18)
        self.assertEqual(len({(rep, case.key(), policy) for rep, case, policy in order}), 216)
        self.assertEqual({case.seed for _, case, _ in order}, {90571, 90583})

    def test_report_packaging_preserves_full_observations(self):
        report = {"experiment": "test", "runs": [{"completed": True}], "case_summaries": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network.json"
            summary = write_report(report, path)
            self.assertNotIn("runs", summary)
            full = path.with_name(summary["full_report"]["file"])
            self.assertEqual(json.loads(gzip.decompress(full.read_bytes())), report)

    def test_setup_failure_is_retained_as_a_failed_attempt(self):
        with patch("handoff_lab.network_experiment.socket.socket", side_effect=OSError("test setup failure")):
            result = run_attempt(self.case(handoffs=2), "cache_delta")
        self.assertFalse(result["completed"])
        self.assertIn("OSError", result["error"])
        self.assertIsNone(result["completion_seconds"])
        self.assertIsNone(result["framed_stream_bytes"])


if __name__ == "__main__":
    unittest.main()
