import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from handoff_lab.model_benchmark import EndpointConfig, call_endpoint, evaluate_answer, summarize


class LocalEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.server.received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.authorization = self.headers.get("Authorization")
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                    return
                if self.path == "/error":
                    self.send_response(429)
                    self.end_headers()
                    self.wfile.write(b"do-not-save-this-error-body")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {"model": "fake-test-model", "choices": [{"message": {"content": '{"ok":true}'}}],
                            "usage": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}}
                self.wfile.write(json.dumps(response).encode())

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def config(self, path="/ok"):
        return EndpointConfig(self.base + path, "test", api_key_env="HANDOFF_TEST_KEY", allow_local_http=True)

    def test_records_real_http_usage_and_never_credentials(self):
        with patch.dict(os.environ, {"HANDOFF_TEST_KEY": "secret-test-key"}):
            result = call_endpoint(self.config(), "system prompt", "user prompt")
        self.assertIsNone(result["error"])
        self.assertEqual(result["model_usage"]["total_tokens"], 22)
        self.assertEqual(result["provider_model"], "fake-test-model")
        self.assertEqual(self.server.authorization, "Bearer secret-test-key")
        self.assertNotIn("secret-test-key", json.dumps(result))
        self.assertGreater(result["latency_seconds"], 0)
        self.assertGreater(result["request_bytes"], 0)

    def test_http_errors_remain_failures_without_error_body(self):
        result = call_endpoint(self.config("/error"), "s", "u")
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["error"], "http_error")
        self.assertIsNone(result["model_usage"])
        self.assertNotIn("do-not-save", json.dumps(result))

    def test_redirect_not_followed(self):
        result = call_endpoint(self.config("/redirect"), "s", "u")
        self.assertEqual(result["http_status"], 302)
        self.assertEqual(result["error"], "http_error")

    def test_http_requires_explicit_loopback(self):
        for endpoint in ("http://example.com/v1/chat/completions", "https://user:pass@example.com/v1",
                         "https://example.com/v1?api_key=secret"):
            with self.assertRaises(ValueError):
                EndpointConfig(endpoint, "test", allow_local_http=True).validate()

    def test_request_options_cannot_replace_messages(self):
        config = self.config()
        config.extra_body = {"messages": []}
        with self.assertRaises(ValueError):
            config.validate()

    def test_strict_answer_validation(self):
        self.assertEqual(evaluate_answer('{"ok":true}', {"ok": True}), (True, None))
        self.assertEqual(evaluate_answer('```json\n{}\n```', {}), (False, "answer_not_json"))
        self.assertEqual(evaluate_answer('{"x":true}', {"x": 1}), (False, "answer_mismatch"))


if __name__ == "__main__":
    unittest.main()
