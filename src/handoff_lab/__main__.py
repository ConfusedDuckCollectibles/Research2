from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .benchmark import demo, run_benchmark
from .model_benchmark import EndpointConfig, run_model_benchmark
from .protocol import parse_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Typed agent handoff research prototype")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "benchmark"):
        command = sub.add_parser(name)
        command.add_argument("--output", type=Path)
    live = sub.add_parser("model-benchmark", help="Makes real network requests; may incur API charges")
    live.add_argument("--endpoint", required=True, help="Full Chat Completions-compatible URL")
    live.add_argument("--model", required=True)
    live.add_argument("--api-key-env", default="OPENAI_API_KEY")
    live.add_argument("--timeout", type=float, default=60)
    live.add_argument("--repetitions", type=int, default=1)
    live.add_argument("--seed", type=int, default=19)
    live.add_argument("--extra-body", default="{}", help="JSON generation options, recorded in results")
    live.add_argument("--allow-local-http", action="store_true")
    live.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "demo":
        result = demo()
    elif args.command == "benchmark":
        result = run_benchmark()
    else:
        config = EndpointConfig(args.endpoint, args.model, args.api_key_env, args.timeout,
                                args.repetitions, args.seed, parse_json(args.extra_body),
                                args.allow_local_http)
        result = run_model_benchmark(config, progress=lambda row: print(
            f"{row['request_order'] + 1}: {row['fixture']} {row['condition']} "
            f"correct={row['correct']} error={row['error'] or row['answer_error'] or 'none'}",
            file=sys.stderr))
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
