"""Run the frozen held-out CLI once; preserve data without interpreting results.

This wrapper does not edit the study or tune policies. Its run-number/attempt
guards deliberately forbid re-execution, including after an infrastructure error.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "latency_execution"
SUMMARY = ROOT / "results/latency.json"
FULL = ROOT / "results/latency_full.json.gz"
PROVENANCE = ROOT / "results/latency_execution.json"
OUTPUTS = (SUMMARY, FULL, PROVENANCE)
SOURCE_COMMIT = "fb2e4aa3e1a76705bd8fc4aaa05d8c293588574a"
SELECTION_COMMIT = "eb6f9966260aec784f8eaa9ab98e3d0761f614f3"
DISCLOSURE_COMMIT = "d4fd08c71a90b6bd14772ca799f4770245181e0e"
TUNING_SHA256 = "fe6746bfb74ea2786398a0522b2e3631f4fa9a207906a443cb47b5f7715f64d3"
TUNING_FULL_SHA256 = "990cdeb9af4fcedfb7d54dad4fecb1f2bda3408440572caa1799ca4aa66f3d9e"
EXPECTED_SELECTION = {
    "miss_guard": {"family": "miss_guard", "interval": 4, "forgetting": None},
    "model": {"family": "latency_model", "interval": 4, "forgetting": 0.5},
    "simple_baseline": "miss_guard_K4",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def remote_main():
    rows = git("ls-remote", "origin", "refs/heads/main").split()
    require(len(rows) == 2, "Cannot identify current main")
    return rows[0]


def run_identity():
    keys = ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME",
            "GITHUB_RUN_ID", "GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT",
            "GITHUB_WORKFLOW", "GITHUB_JOB", "RUNNER_OS", "RUNNER_ARCH",
            "ImageOS", "ImageVersion")
    return {key: os.environ.get(key) for key in keys}


def guard_identity():
    require(os.environ.get("GITHUB_REPOSITORY") == "ConfusedDuckCollectibles/Research2", "Unexpected repository")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Only main may execute")
    require(os.environ.get("GITHUB_EVENT_NAME") == "push", "Only the reviewed publication push may execute")
    require(os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "Reruns are forbidden")
    require(os.environ.get("GITHUB_RUN_NUMBER") == "1", "Only this new workflow's first run may measure")
    require(platform.python_version() == "3.12.14", "Python 3.12.14 is required")
    expected = os.environ["GITHUB_SHA"]
    require(git("rev-parse", "HEAD") == expected, "Checkout differs from workflow commit")
    require(remote_main() == expected, "Main advanced; stop without retry")
    return expected


def frozen_inputs():
    sys.path.insert(0, str(ROOT / "src"))
    from handoff_lab.latency_experiment import load_selection, source_hashes
    tuning = ROOT / "results/latency_selection.json"
    require(sha(tuning.read_bytes()) == TUNING_SHA256, "Tuning summary differs from the published bytes")
    require(sha((ROOT / "results/latency_selection_full.json.gz").read_bytes()) == TUNING_FULL_SHA256,
            "Tuning complete observations differ from the published bytes")
    selected, source = load_selection(tuning)
    require(source == SOURCE_COMMIT and selected == EXPECTED_SELECTION, "Frozen source or selected settings differ")
    note = "docs/latency_execution_amendment.md"
    pinned_note = subprocess.check_output(["git", "show", DISCLOSURE_COMMIT + ":" + note], cwd=ROOT)
    require((ROOT / note).read_bytes() == pinned_note, "Published host disclosure changed")
    subprocess.run(["git", "merge-base", "--is-ancestor", DISCLOSURE_COMMIT, "HEAD"], cwd=ROOT, check=True)
    return {"source_hashes": source_hashes(), "selection": selected,
            "execution_note_sha256": sha(pinned_note)}


def logged(command, filename):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    with (LOGS / filename).open("w") as output:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            output.write(line)
            output.flush()
            print(line, end="", flush=True)
        return process.wait()


def verified_results(inputs):
    summary_bytes, packed = SUMMARY.read_bytes(), FULL.read_bytes()
    summary = json.loads(summary_bytes)
    full = json.loads(gzip.decompress(packed))
    metadata = summary["full_report"]
    require(metadata["file"] == FULL.name and metadata["sha256"] == sha(packed)
            and metadata["compressed_bytes"] == len(packed), "Held-out archive identity mismatch")
    require(metadata["uncompressed_bytes"] == len(gzip.decompress(packed)), "Held-out uncompressed size mismatch")
    for key in ("experiment", "split", "source_commit", "design_commit", "selection_commit",
                "selection", "planned_runs", "completed_runs", "failed_runs", "missing_runs", "source_hashes"):
        require(summary[key] == full[key], "Summary/report disagreement: " + key)
    require(full["experiment"] == "causal_completion_time_v1" and full["split"] == "heldout", "Wrong experiment")
    require(full["source_commit"] == SOURCE_COMMIT and full["selection_commit"] == SELECTION_COMMIT,
            "Wrong source/selection provenance")
    require(full["selection"] == EXPECTED_SELECTION and full["source_hashes"] == inputs["source_hashes"],
            "Study settings or source changed")
    from handoff_lab.latency_experiment import execution_order
    expected = execution_order("heldout", EXPECTED_SELECTION)
    rows = full["runs"]
    require(len(expected) == len(rows) == full["planned_runs"] == 360, "Incomplete raw cell ledger")
    for ordinal, (row, (repetition, case, policy)) in enumerate(zip(rows, expected)):
        require(row["execution_ordinal"] == ordinal and row["repetition"] == repetition
                and row["case"] == case.to_dict() and row["policy"] == policy.name, "Raw execution order differs")
    # Failed/missing cells remain valid publication data; no success gate filters them.
    return {"summary_sha256": sha(summary_bytes), "full_sha256": sha(packed),
            "summary_bytes": len(summary_bytes), "full_bytes": len(packed),
            "planned_runs": full["planned_runs"], "completed_runs": full["completed_runs"],
            "failed_runs": full["failed_runs"], "missing_runs": full["missing_runs"]}


def run_once():
    LOGS.mkdir(parents=True, exist_ok=True)
    metadata = {"schema_version": 1, "started_utc": now(), "status": "preflight",
        "workflow": run_identity(), "source_commit": SOURCE_COMMIT,
        "selection_commit": SELECTION_COMMIT, "execution_disclosure_commit": DISCLOSURE_COMMIT,
        "wrapper_sha256": sha(Path(__file__).read_bytes()), "python": sys.version,
        "platform": platform.platform(), "cpu_count": os.cpu_count(),
        "measurement_started": False,
        "host_condition": "Tuning used the workspace host; held-out uses a clean GitHub CPU runner"}
    write_json(LOGS / "attempt.json", metadata)
    try:
        guard_identity()
        require(not any(path.exists() or path.is_symlink() for path in OUTPUTS), "Held-out outputs already exist")
        inputs = frozen_inputs()
        metadata.update(inputs)
        test_start = time.perf_counter()
        tests = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
        metadata["unit_test_return_code"] = logged(tests, "unit_tests.log")
        metadata["unit_test_seconds"] = time.perf_counter() - test_start
        require(metadata["unit_test_return_code"] == 0, "Unit tests failed; no measurement starts")
        # Repeat identity/input checks after tests and immediately before measuring.
        guard_identity()
        require(not any(path.exists() or path.is_symlink() for path in OUTPUTS), "Held-out outputs appeared")
        require(frozen_inputs() == inputs, "Frozen inputs changed during tests")
        command = [sys.executable, "-m", "handoff_lab.latency_experiment", "evaluate",
                   "--selection", "results/latency_selection.json", "--selection-commit", SELECTION_COMMIT,
                   "--output", "results/latency.json"]
        metadata.update({"measurement_started": True, "measurement_started_utc": now(),
                         "status": "measuring", "command": command})
        write_json(PROVENANCE, metadata)
        write_json(LOGS / "attempt.json", metadata)
        metadata["cli_return_code"] = logged(command, "measurement.log")
        metadata["measurement_finished_utc"] = now()
        require(metadata["cli_return_code"] == 0, "Frozen CLI failed; preserve partial outputs without retry")
        metadata["outputs"] = verified_results(inputs)
        metadata["status"] = "raw_outputs_verified"
    except Exception as exc:
        metadata["status"] = "failed_without_retry"
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        metadata["finished_utc"] = now()
        write_json(LOGS / "attempt.json", metadata)
        if metadata["measurement_started"]:
            write_json(PROVENANCE, metadata)


def publish():
    LOGS.mkdir(parents=True, exist_ok=True)
    receipt = {"started_utc": now(), "workflow": run_identity(), "published": False}
    try:
        expected = guard_identity()
        metadata = json.loads(PROVENANCE.read_text())
        require(metadata["workflow"] == run_identity() and metadata["status"] == "raw_outputs_verified",
                "This run has no verified raw outputs to publish")
        require(verified_results(frozen_inputs()) == metadata["outputs"], "Raw output bytes changed")
        require(not git("diff", "--cached", "--name-only"), "Unexpected pre-existing staged changes")
        paths = [str(path.relative_to(ROOT)) for path in OUTPUTS]
        git("add", "--", *paths)
        require(set(git("diff", "--cached", "--name-only").splitlines()) == set(paths),
                "Only the three new result/provenance paths may be committed")
        git("-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", "Preserve the single frozen held-out latency run and execution provenance")
        require(remote_main() == expected, "Main advanced before push; retain artifact without retry")
        git("push", "origin", "HEAD:main")  # Normal push; no force, reset, merge or retry.
        receipt.update({"published": True, "published_commit": git("rev-parse", "HEAD")})
        print(json.dumps(receipt), flush=True)
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        receipt["finished_utc"] = now()
        write_json(LOGS / "publication.json", receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "publish"))
    args = parser.parse_args()
    (run_once if args.mode == "run" else publish)()


if __name__ == "__main__":
    main()
