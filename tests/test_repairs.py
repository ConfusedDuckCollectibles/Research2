import unittest

from handoff_lab.repair_tasks import (TASKS, UnsupportedProgram, assess_source,
    parse_program, self_check, assess_response, repair_prompt, run_repair_benchmark)
from handoff_lab.model_benchmark import EndpointConfig
import json


class RepairEvaluatorTests(unittest.TestCase):
    def test_all_reference_fixes_pass_and_all_originals_fail(self):
        self.assertTrue(self_check()["success"])

    def test_unsupported_effects_are_rejected_before_evaluation(self):
        samples = [
            "import os\ndef solve(n):\n    return 0",
            "def solve(n):\n    return __import__('os').system('false')",
            "def solve(n):\n    return n.__class__",
            "def solve(n):\n    return (lambda: n)()",
            "def solve(n):\n    while True: pass",
            "@print\ndef solve(n):\n    return n",
            "def solve(n=1):\n    return n",
            "def solve(n):\n    return 2 ** n",
        ]
        for source in samples:
            with self.subTest(source=source), self.assertRaises(UnsupportedProgram):
                parse_program(source, ("n",))

    def test_unselected_branch_is_still_validated(self):
        with self.assertRaises(UnsupportedProgram):
            parse_program("def solve(n):\n    return n if True else open('anything')", ("n",))

    def test_wrong_signature_fails(self):
        self.assertFalse(assess_source(TASKS[2], "def solve(n):\n    return n")["valid_program"])

    def test_partial_fix_does_not_count_as_success(self):
        result = assess_source(TASKS[2], "def solve(n, d):\n    return n // d + 1")
        self.assertGreater(result["passed"], 0)
        self.assertFalse(result["success"])

    def test_short_circuit_is_respected(self):
        source = "def solve(n, d):\n    return 0 if n == 0 else (n + d - 1) // d"
        self.assertTrue(assess_source(TASKS[2], source)["success"])

    def test_bool_return_contract_is_checked(self):
        result = assess_source(TASKS[1], "def solve(year):\n    return 1")
        self.assertEqual(result["passed"], 0)

    def test_bounded_numeric_and_source_inputs(self):
        for expression in ("1e999", "1000000000001", "'no'", "None"):
            with self.subTest(expression=expression), self.assertRaises(UnsupportedProgram):
                parse_program("def solve(n):\n    return " + expression, ("n",))
        with self.assertRaises(UnsupportedProgram):
            parse_program(" " * 8001, ("n",))

    def test_reference_solution_is_not_in_prompt(self):
        for task in TASKS:
            for language in ("english", "structured"):
                for storage in ("inline", "shared"):
                    system, user = repair_prompt(task, language, storage)
                    self.assertNotIn(task.reference_source.strip(), user)
                    self.assertNotIn(json.dumps(task.reference_source), user)
                    self.assertNotIn("cases", user)

    def test_response_requires_exact_json_shape(self):
        for content in (None, "not JSON", "[]", '{"source":7}', '{"source":"x","extra":1}'):
            self.assertFalse(assess_response(TASKS[0], content)["success"])

    def test_mock_endpoint_is_labeled_and_errors_stay_in_denominator(self):
        def fake(config, system, user):
            return {"content": None, "error": "transport_error", "model_usage": None,
                    "latency_seconds": 0.01, "request_bytes": 1, "response_bytes": None}
        result = run_repair_benchmark(EndpointConfig("https://example.invalid/api", "test"), fake)
        self.assertEqual(result["execution"], "custom_test_caller")
        self.assertEqual(result["requests_attempted"], 24)
        self.assertEqual(sum(row["attempts"] for row in result["summary"]), 24)
        self.assertEqual(sum(row["correct"] for row in result["summary"]), 0)

    def test_success_is_based_on_acceptance_cases(self):
        calls = [0]
        def fake(config, system, user):
            task = TASKS[calls[0] // 4]
            calls[0] += 1
            return {"content": json.dumps({"source": task.reference_source}), "error": None,
                    "model_usage": None, "latency_seconds": 0.01,
                    "request_bytes": 1, "response_bytes": 1}
        result = run_repair_benchmark(EndpointConfig("https://example.invalid/api", "test"), fake)
        self.assertEqual(sum(row["correct"] for row in result["summary"]), 24)
        self.assertTrue(all(row["assessment"]["success"] for row in result["records"]))


if __name__ == "__main__":
    unittest.main()
