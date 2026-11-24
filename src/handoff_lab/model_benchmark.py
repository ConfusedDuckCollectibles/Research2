"""Explicit opt-in live receiver assay using a Chat Completions-style endpoint.

No requests are made by deterministic benchmarks or imports. Credentials are read
from an environment variable and never placed in saved results. Redirects are
rejected to avoid forwarding authorization to another location.
"""

from __future__ import annotations

import hashlib
import os
import platform
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .benchmark import CASES, CONDITIONS, build_prompt, make_fixture
from .protocol import ProtocolError, canonical, parse_json


@dataclass
class EndpointConfig:
    endpoint: str
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    timeout: float = 60.0
    repetitions: int = 1
    seed: int = 19
    extra_body: dict = field(default_factory=dict)
    allow_local_http: bool = False

    def validate(self) -> None:
        parsed = urllib.parse.urlsplit(self.endpoint)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Endpoint must be a complete URL without credentials, query, or fragment")
        if parsed.scheme != "https":
            if not (self.allow_local_http and parsed.scheme == "http" and
                    parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("HTTPS required; HTTP is permitted only for explicit localhost tests")
        if type(self.model) is not str or not self.model.strip():
            raise ValueError("A model identifier is required")
        if not 0 < self.timeout <= 300:
            raise ValueError("Timeout must be between 0 and 300 seconds")
        if type(self.repetitions) is not int or self.repetitions < 1:
            raise ValueError("Repetitions must be a positive integer")
        if type(self.extra_body) is not dict or set(self.extra_body) & {"model", "messages", "stream"}:
            raise ValueError("extra_body must be an object and cannot override model/messages/stream")
        canonical(self.extra_body)

    def metadata(self) -> dict:
        return {"endpoint": self.endpoint, "model_requested": self.model,
                "api_key_environment_variable": self.api_key_env, "timeout_seconds": self.timeout,
                "repetitions": self.repetitions, "order_seed": self.seed,
                "extra_body": self.extra_body, "stream": False,
                "retries_per_request": 0, "python_version": platform.python_version()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call_endpoint(config: EndpointConfig, system: str, user: str) -> dict:
    config.validate()
    payload = {**config.extra_body, "model": config.model, "stream": False,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    encoded = canonical(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    secret = os.environ.get(config.api_key_env, "") if config.api_key_env else ""
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    request = urllib.request.Request(config.endpoint, data=encoded, headers=headers, method="POST")
    record = {"started_utc": datetime.now(timezone.utc).isoformat(),
              "request_sha256": hashlib.sha256(encoded).hexdigest(),
              "request_bytes": len(encoded), "response_bytes": None,
              "latency_seconds": None, "http_status": None, "provider_model": None,
              "model_usage": None, "content": None, "error": None,
              "retry_count": 0}
    started = time.perf_counter()
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=config.timeout) as response:
            raw = response.read(2_000_001)
            record["http_status"] = response.status
        record["response_bytes"] = len(raw)
        record["latency_seconds"] = time.perf_counter() - started
        if len(raw) > 2_000_000:
            record["error"] = "response_too_large"
            return record
        parsed = parse_json(raw.decode("utf-8"))
        if type(parsed) is not dict:
            raise ProtocolError("API response must be an object")
        record["provider_model"] = parsed.get("model")
        if type(parsed.get("usage")) is dict:
            record["model_usage"] = parsed["usage"]
        content = parsed["choices"][0]["message"]["content"]
        if type(content) is not str:
            record["error"] = "missing_message_content"
        else:
            record["content"] = content
    except urllib.error.HTTPError as exc:
        record["http_status"] = exc.code
        record["error"] = "http_error"
        # Do not save error bodies: some gateways echo credential-bearing requests.
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        record["error"] = "transport_error"
    except (ProtocolError, UnicodeError, KeyError, IndexError, TypeError):
        record["error"] = "invalid_api_response"
    finally:
        if record["latency_seconds"] is None:
            record["latency_seconds"] = time.perf_counter() - started
    return record


def evaluate_answer(content: str | None, expected: dict) -> tuple[bool, str | None]:
    if content is None:
        return False, "no_answer"
    try:
        answer = parse_json(content)
    except ProtocolError:
        return False, "answer_not_json"
    if canonical(answer) != canonical(expected):
        return False, "answer_mismatch"
    return True, None


def summarize(records: list[dict]) -> list[dict]:
    result = []
    for language, storage in CONDITIONS:
        condition = f"{language}_{storage}"
        rows = [record for record in records if record["condition"] == condition]
        usage_rows = [record for record in rows if type(record.get("model_usage")) is dict]
        token_counts = {}
        for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [record["model_usage"].get(field_name) for record in usage_rows]
            observed = [value for value in values if type(value) is int and value >= 0]
            token_counts[field_name] = {"observed_sum": sum(observed) if observed else None,
                                        "requests_reporting": len(observed)}
        latencies = [record["latency_seconds"] for record in rows]
        result.append({"condition": condition, "attempts": len(rows),
                       "correct": sum(record["correct"] for record in rows),
                       "request_errors": sum(record["error"] is not None for record in rows),
                       "answer_errors": sum(record["answer_error"] is not None for record in rows),
                       "mean_latency_seconds": statistics.mean(latencies) if latencies else None,
                       "median_latency_seconds": statistics.median(latencies) if latencies else None,
                       "provider_reported_tokens": token_counts, "billed_cost": None})
    return result


def run_model_benchmark(config: EndpointConfig,
                        progress: Callable[[dict], None] | None = None) -> dict:
    config.validate()
    rng = random.Random(config.seed)
    records = []
    for repetition in range(config.repetitions):
        for case in CASES:
            fixture = make_fixture(case)
            order = list(CONDITIONS)
            rng.shuffle(order)
            for language, storage in order:
                system, user = build_prompt(fixture, language, storage)
                record = call_endpoint(config, system, user)
                correct, answer_error = evaluate_answer(record["content"], fixture.expected_answer)
                record.update({"fixture": fixture.task_id,
                               "condition": f"{language}_{storage}", "repetition": repetition,
                               "request_order": len(records), "correct": correct,
                               "answer_error": answer_error,
                               "expected_answer": fixture.expected_answer})
                records.append(record)
                if progress:
                    progress(record)
    return {"schema_version": 1, "experiment": "live_receiver_comprehension",
            "config": config.metadata(), "model_requests_attempted": len(records),
            "limitations": [
                "This tests comprehension of supplied synthetic handoffs, not autonomous coding success.",
                "Shared prompts include initial state and deltas: nothing is supplied free outside context.",
                "Provider token usage is recorded verbatim when available; no tokens are estimated.",
                "Generation settings are identical across conditions; default provider behavior is recorded via config.",
                "Request order is seeded and shuffled within each fixture; server caching remains uncontrolled.",
                "No retries occur. Failures remain in denominators; non-JSON answers fail exact validation.",
                "No pricing, token savings, or inference-speed conclusion is inferred from byte counts.",
                "The three fixtures are development examples, not a representative held-out benchmark."],
            "summary": summarize(records), "records": records}
