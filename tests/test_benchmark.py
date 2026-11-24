import copy
import unittest

from handoff_lab.benchmark import (CASES, CONDITIONS, build_prompt, make_fixture,
                                   measure, replay_store, run_benchmark)
from handoff_lab.protocol import ProtocolError, canonical


class BenchmarkTests(unittest.TestCase):
    def test_all_conditions_preserve_semantics(self):
        report = run_benchmark()
        self.assertEqual(len(report["rows"]), 12)
        self.assertTrue(report["all_semantic_roundtrips_exact"])
        self.assertTrue(report["all_deterministic_reads_exact"])
        self.assertEqual(report["model_calls_performed"], 0)
        self.assertIsNone(report["tokenizer_used"])
        for row in report["rows"]:
            self.assertIsNone(row["model_tokens"])
            self.assertIsNone(row["model_success"])

    def test_all_byte_totals_are_exact_sums(self):
        report = run_benchmark()
        for total in report["totals"]:
            rows = [row for row in report["rows"] if row["condition"] == total["condition"]]
            keys = [key for key in rows[0] if "_bytes" in key]
            for key in keys:
                self.assertEqual(total[key], sum(row[key] for row in rows))

    def test_no_free_shared_state_or_dereferences(self):
        fixture = make_fixture(CASES[0])
        shared = measure(fixture, "structured", "shared")
        inline = measure(fixture, "structured", "inline")
        self.assertGreater(shared["publisher_store_setup_bytes"], 0)
        self.assertGreater(shared["publisher_store_update_bytes"], 0)
        self.assertGreater(shared["consumer_read_bytes_revision_cache"], 0)
        self.assertEqual(shared["consumer_read_bytes_no_cache"],
                         2 * shared["consumer_read_bytes_revision_cache"])
        self.assertEqual(shared["total_traffic_bytes_no_cache"],
                         shared["agent_message_bytes"] + shared["publisher_store_write_bytes"] +
                         shared["consumer_read_bytes_no_cache"])
        self.assertEqual(inline["publisher_store_write_bytes"], 0)
        self.assertEqual(inline["consumer_read_bytes_no_cache"], 0)
        # Fully resolving all attachments gives the same content and representation.
        self.assertEqual(shared["expanded_handoff_bytes"], inline["expanded_handoff_bytes"])

    def test_prompt_includes_state_and_no_expected_answer(self):
        fixture = make_fixture(CASES[0])
        _, shared = build_prompt(fixture, "english", "shared")
        self.assertIn(canonical(fixture.store_events), shared)
        self.assertNotIn("expected_answer", shared)

    def test_store_transcript_integrity_is_checked(self):
        fixture = make_fixture(CASES[0])
        bad = copy.deepcopy(fixture.store_events)
        bad[1]["result"]["sha256"] = "0" * 64
        with self.assertRaises(ProtocolError):
            replay_store(bad)

    def test_report_is_reproducible(self):
        self.assertEqual(run_benchmark(), run_benchmark())


if __name__ == "__main__":
    unittest.main()
