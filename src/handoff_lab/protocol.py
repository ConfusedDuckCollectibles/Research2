"""Data-only protocol: checked references, immutable revisions, atomic field deltas.

Canonicalization here means this package's deterministic Python JSON encoding,
not RFC 8785. Hashes establish content integrity, not identity or authorization.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

MAX_MESSAGE_BYTES = 2_000_000
OPS = frozenset({"request", "result", "update", "question", "error"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()


class ProtocolError(ValueError):
    """A message or store operation violates the explicit protocol contract."""


class MissingArtifact(ProtocolError):
    pass


class MissingRevision(ProtocolError):
    pass


class DigestMismatch(ProtocolError):
    pass


class StaleRevision(ProtocolError):
    pass


def _json_value(value: Any) -> None:
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_value(item)
        return
    raise ProtocolError("Only JSON values with string object keys are supported")


def canonical(value: Any) -> str:
    """Stable UTF-8-compatible JSON for this implementation; reject nonfinite floats."""
    try:
        _json_value(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")  # Reject lone surrogates, including escaped JSON inputs.
        return encoded
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError(str(exc)) from exc


def nbytes(value: str) -> int:
    if type(value) is not str:
        raise ProtocolError("Text must be a string")
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ProtocolError("Text must be valid UTF-8") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(text: str) -> Any:
    if nbytes(text) > MAX_MESSAGE_BYTES:
        raise ProtocolError("Message exceeds the 2 MB limit")
    try:
        result = json.loads(text, object_pairs_hook=_object_pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(
                                ProtocolError(f"Invalid JSON constant: {value}")))
        canonical(result)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProtocolError(str(exc)) from exc


def _keys(value: Any, required: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != required:
        raise ProtocolError(f"{label} requires exactly these keys: {sorted(required)}")


def _identifier(value: Any, label: str) -> None:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ProtocolError(f"Invalid {label}; use 1–128 ASCII identifier characters")


@dataclass(frozen=True)
class Ref:
    artifact_id: str
    revision: int
    sha256: str

    def __post_init__(self) -> None:
        _identifier(self.artifact_id, "artifact id")
        if type(self.revision) is not int or self.revision < 1:
            raise ProtocolError("Revision must be a positive integer")
        if type(self.sha256) is not str or not _HASH.fullmatch(self.sha256):
            raise ProtocolError("sha256 must contain 64 lowercase hexadecimal characters")

    def to_dict(self) -> dict:
        return {"id": self.artifact_id, "rev": self.revision, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: dict) -> "Ref":
        _keys(value, {"id", "rev", "sha256"}, "Reference")
        return cls(value["id"], value["rev"], value["sha256"])


@dataclass(frozen=True)
class Delta:
    """Atomic replacement/removal of top-level object fields; not JSON Patch."""

    base: Ref
    set_fields: dict
    remove_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.base, Ref) or type(self.set_fields) is not dict:
            raise ProtocolError("Delta requires a reference and a set-fields object")
        canonical(self.set_fields)
        if type(self.remove_fields) is not tuple or not all(
                type(key) is str for key in self.remove_fields):
            raise ProtocolError("remove_fields must be a tuple of strings")
        if len(set(self.remove_fields)) != len(self.remove_fields):
            raise ProtocolError("Duplicate removed field")
        if set(self.set_fields) & set(self.remove_fields):
            raise ProtocolError("Cannot set and remove the same field")
        if not self.set_fields and not self.remove_fields:
            raise ProtocolError("Empty delta")
        object.__setattr__(self, "set_fields", copy.deepcopy(self.set_fields))

    def to_dict(self) -> dict:
        # Frozen dataclasses are shallow; revalidate externally mutable containers.
        self.__post_init__()
        return {"base": self.base.to_dict(), "set": copy.deepcopy(self.set_fields),
                "remove": list(self.remove_fields)}

    @classmethod
    def from_dict(cls, value: dict) -> "Delta":
        _keys(value, {"base", "set", "remove"}, "Delta")
        if type(value["remove"]) is not list:
            raise ProtocolError("Delta remove must be an array")
        return cls(Ref.from_dict(value["base"]), value["set"], tuple(value["remove"]))


class ArtifactStore:
    """An in-process immutable-revision store. Not a durable/concurrent database."""

    def __init__(self) -> None:
        self._items: dict[str, list[dict]] = {}

    def add(self, artifact_id: str, value: dict) -> Ref:
        _identifier(artifact_id, "artifact id")
        if artifact_id in self._items:
            raise ProtocolError(f"Artifact already exists: {artifact_id}")
        if type(value) is not dict:
            raise ProtocolError("Artifact must be a JSON object")
        content_hash = digest(value)
        self._items[artifact_id] = [copy.deepcopy(value)]
        return Ref(artifact_id, 1, content_hash)

    def read(self, ref: Ref) -> dict:
        if not isinstance(ref, Ref):
            raise ProtocolError("read requires a validated Ref")
        history = self._items.get(ref.artifact_id)
        if history is None:
            raise MissingArtifact(ref.artifact_id)
        if ref.revision > len(history):
            raise MissingRevision(f"{ref.artifact_id}@{ref.revision}")
        value = history[ref.revision - 1]
        if digest(value) != ref.sha256:
            raise DigestMismatch(f"Digest mismatch: {ref.artifact_id}@{ref.revision}")
        return copy.deepcopy(value)

    def apply(self, delta: Delta) -> Ref:
        # Validate everything before publishing the new revision.
        if not isinstance(delta, Delta):
            raise ProtocolError("apply requires a Delta")
        delta = Delta.from_dict(delta.to_dict())
        value = self.read(delta.base)
        if len(self._items[delta.base.artifact_id]) != delta.base.revision:
            raise StaleRevision(f"Update requires latest revision of {delta.base.artifact_id}")
        canonical(delta.set_fields)
        for key in delta.remove_fields:
            if key not in value:
                raise ProtocolError(f"Cannot remove nonexistent field: {key}")
        for key in delta.remove_fields:
            del value[key]
        value.update(copy.deepcopy(delta.set_fields))
        result = Ref(delta.base.artifact_id, delta.base.revision + 1, digest(value))
        self._items[delta.base.artifact_id].append(value)
        return result


def attachment(ref: Ref, value: Any = _MISSING) -> dict:
    if not isinstance(ref, Ref):
        raise ProtocolError("Attachment requires a validated Ref")
    result = {"ref": ref.to_dict()}
    if value is not _MISSING:
        if type(value) is not dict or digest(value) != ref.sha256:
            raise DigestMismatch("Inline value does not match reference")
        result["value"] = copy.deepcopy(value)
    return result


@dataclass(frozen=True)
class Envelope:
    task: str
    op: str
    sender: str
    recipient: str
    body: dict
    artifacts: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        for value, label in ((self.task, "task"), (self.sender, "sender"),
                             (self.recipient, "recipient")):
            _identifier(value, label)
        if type(self.op) is not str or self.op not in OPS:
            raise ProtocolError(f"Unsupported operation: {self.op}")
        if type(self.body) is not dict:
            raise ProtocolError("Body must be an object")
        canonical(self.body)
        if type(self.artifacts) is not tuple:
            raise ProtocolError("artifacts must be a tuple")
        for item in self.artifacts:
            if type(item) is not dict or set(item) not in ({"ref"}, {"ref", "value"}):
                raise ProtocolError("Attachment requires ref and optional value")
            ref = Ref.from_dict(item["ref"])
            if "value" in item:
                attachment(ref, item["value"])
        object.__setattr__(self, "body", copy.deepcopy(self.body))
        object.__setattr__(self, "artifacts", copy.deepcopy(self.artifacts))

    def to_dict(self) -> dict:
        self.__post_init__()
        return {"v": 1, "task": self.task, "op": self.op, "from": self.sender,
                "to": self.recipient, "body": copy.deepcopy(self.body),
                "artifacts": list(copy.deepcopy(self.artifacts))}

    def encode(self) -> str:
        encoded = canonical(self.to_dict())
        if nbytes(encoded) > MAX_MESSAGE_BYTES:
            raise ProtocolError("Message exceeds the 2 MB limit")
        return encoded

    @classmethod
    def decode(cls, text: str) -> "Envelope":
        value = parse_json(text)
        _keys(value, {"v", "task", "op", "from", "to", "body", "artifacts"}, "Envelope")
        if type(value["v"]) is not int or value["v"] != 1:
            raise ProtocolError("Unsupported protocol version")
        if type(value["artifacts"]) is not list:
            raise ProtocolError("artifacts must be an array")
        return cls(value["task"], value["op"], value["from"], value["to"],
                   value["body"], tuple(value["artifacts"]))

    def materialize(self, store: ArtifactStore) -> dict:
        value = self.to_dict()
        resolved = []
        for item in self.artifacts:
            ref = Ref.from_dict(item["ref"])
            content = copy.deepcopy(item["value"]) if "value" in item else store.read(ref)
            if digest(content) != ref.sha256:
                raise DigestMismatch("Attachment changed after validation")
            resolved.append(attachment(ref, content))
        value["artifacts"] = resolved
        return value


def render_audit(message: Envelope, store: ArtifactStore) -> str:
    """Deterministic, fully resolved record; no model-written interpretation."""
    return json.dumps(message.materialize(store), ensure_ascii=False,
                      sort_keys=True, indent=2, allow_nan=False)
