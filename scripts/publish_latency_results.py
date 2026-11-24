"""Materialize an already measured, reviewed report from bounded byte parts.

This packaging helper never executes the experiment or changes its observations.
The workflow uses a normal fast-forward push and stops if main has advanced.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "publication" / "latency_bundle.json"
ALLOWED = {
    "tuning": ("latency_selection.json", "latency_selection_full.json.gz"),
    "heldout": ("latency.json", "latency_full.json.gz"),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()


def prepare():
    manifest = json.loads(MANIFEST.read_text())
    summary_name, full_name = ALLOWED[manifest["stage"]]
    if manifest["schema_version"] != 1 or manifest["summary_file"] != summary_name or manifest["full_file"] != full_name:
        raise ValueError("Unsupported publication manifest")
    summary_path = ROOT / "results" / summary_name
    summary_bytes = summary_path.read_bytes()
    if sha(summary_bytes) != manifest["summary_sha256"]:
        raise ValueError("Published summary checksum differs")
    summary = json.loads(summary_bytes)
    destination = ROOT / "results" / full_name
    parts = []
    for index, item in enumerate(manifest["parts"]):
        expected = f"publication/latency_parts/{manifest['stage']}-{index:03d}.part"
        if item["path"] != expected:
            raise ValueError("Unexpected part path or order")
        parts.append((ROOT / expected, item))
    if destination.is_file():
        packed = destination.read_bytes()
    else:
        contents = []
        for path, item in parts:
            data = path.read_bytes()
            if len(data) != item["bytes"] or sha(data) != item["sha256"]:
                raise ValueError("Part checksum differs")
            contents.append(data)
        packed = b"".join(contents)
    if len(packed) != manifest["bytes"] or sha(packed) != manifest["sha256"]:
        raise ValueError("Complete report checksum differs")
    metadata = summary["full_report"]
    if metadata["file"] != full_name or metadata["sha256"] != manifest["sha256"] or metadata["compressed_bytes"] != len(packed):
        raise ValueError("Summary and complete report identity differ")
    raw = gzip.decompress(packed)
    if len(raw) != metadata["uncompressed_bytes"]:
        raise ValueError("Uncompressed size differs")
    full = json.loads(raw)
    for key in ("experiment", "split", "source_commit", "design_commit", "selection", "planned_runs", "completed_runs", "failed_runs", "missing_runs", "source_hashes"):
        if full[key] != summary[key]:
            raise ValueError("Summary/report disagreement: " + key)
    if full["experiment"] != "causal_completion_time_v1" or full["split"] != manifest["stage"]:
        raise ValueError("Unexpected experiment or stage")
    for name, expected in full["source_hashes"].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or sha(path.read_bytes()) != expected:
            raise ValueError("Frozen study file changed: " + name)
    return manifest, destination, packed, [path for path, _ in parts]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    manifest, destination, packed, parts = prepare()
    print(json.dumps({"stage": manifest["stage"], "bytes": len(packed), "sha256": sha(packed), "packaging_only": True}))
    if args.check_only:
        return
    expected = os.environ["GITHUB_SHA"]
    if git("rev-parse", "HEAD") != expected or git("ls-remote", "origin", "refs/heads/main").split()[0] != expected:
        raise RuntimeError("Main changed; preserve data and stop without retry or force")
    destination.write_bytes(packed)
    for path in parts:
        path.unlink(missing_ok=True)
    tracked_parts = [str(path.relative_to(ROOT)) for path in parts
                     if git("ls-files", "--", str(path.relative_to(ROOT)))]
    git("add", "--", str(destination.relative_to(ROOT)), *tracked_parts)
    if not git("diff", "--cached", "--name-only"):
        print("Verified report already published; no commit needed")
        return
    allowed = {str(destination.relative_to(ROOT)), *(str(path.relative_to(ROOT)) for path in parts)}
    if not set(git("diff", "--cached", "--name-only").splitlines()) <= allowed:
        raise RuntimeError("Unexpected staged paths")
    git("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", f"Preserve complete audited latency {manifest['stage']} report without rerunning experiments")
    if git("ls-remote", "origin", "refs/heads/main").split()[0] != expected:
        raise RuntimeError("Main advanced before push; no retry or force")
    git("push", "origin", "HEAD:main")
    print(json.dumps({"published_commit": git("rev-parse", "HEAD")}))


if __name__ == "__main__":
    main()
