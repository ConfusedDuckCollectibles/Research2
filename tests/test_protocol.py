import copy
import json
import unittest

from handoff_lab.protocol import (ArtifactStore, Delta, DigestMismatch, Envelope,
                                 MissingArtifact, MissingRevision, ProtocolError,
                                 Ref, StaleRevision, attachment, canonical, digest,
                                 parse_json, render_audit)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = ArtifactStore()
        self.source = {"source": "return 1", "checks": [1, 2], "obsolete": True}
        self.first = self.store.add("code/example", self.source)

    def test_reads_and_original_input_do_not_mutate_store(self):
        self.source["checks"].append(3)
        observed = self.store.read(self.first)
        observed["checks"].clear()
        self.assertEqual(self.store.read(self.first)["checks"], [1, 2])

    def test_delta_preserves_old_revision_and_removes_field(self):
        second = self.store.apply(Delta(self.first, {"source": "return 2"}, ("obsolete",)))
        self.assertEqual(second.revision, 2)
        self.assertEqual(self.store.read(self.first)["source"], "return 1")
        self.assertEqual(self.store.read(second), {"source": "return 2", "checks": [1, 2]})

    def test_stale_update_rejected(self):
        self.store.apply(Delta(self.first, {"x": 1}))
        with self.assertRaises(StaleRevision):
            self.store.apply(Delta(self.first, {"x": 2}))

    def test_invalid_delta_is_atomic(self):
        with self.assertRaises(ProtocolError):
            self.store.apply(Delta(self.first, {"source": "broken"}, ("missing",)))
        second = self.store.apply(Delta(self.first, {"source": "safe"}))
        self.assertEqual(second.revision, 2)
        self.assertEqual(self.store.read(self.first)["source"], "return 1")

    def test_mutated_delta_cannot_bypass_conflict_validation(self):
        delta = Delta(self.first, {"source": "return 2"}, ("obsolete",))
        delta.set_fields["obsolete"] = False
        with self.assertRaises(ProtocolError):
            self.store.apply(delta)
        self.assertEqual(self.store.read(self.first), self.source)

    def test_reference_failures_are_distinct(self):
        with self.assertRaises(MissingArtifact):
            self.store.read(Ref("unknown", 1, self.first.sha256))
        with self.assertRaises(MissingRevision):
            self.store.read(Ref(self.first.artifact_id, 9, self.first.sha256))
        with self.assertRaises(DigestMismatch):
            self.store.read(Ref(self.first.artifact_id, 1, "0" * 64))

    def test_conflicting_and_duplicate_field_operations_rejected(self):
        with self.assertRaises(ProtocolError):
            Delta(self.first, {"x": 1}, ("x",))
        with self.assertRaises(ProtocolError):
            Delta(self.first, {}, ("x", "x"))


class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.store = ArtifactStore()
        self.ref = self.store.add("test", {"content": "αβ", "checks": [True, None]})
        self.message = Envelope("task-1", "result", "a", "b", {"ok": True}, (attachment(self.ref),))

    def test_roundtrip_and_deterministic_audit(self):
        decoded = Envelope.decode(self.message.encode())
        self.assertEqual(decoded.to_dict(), self.message.to_dict())
        audit = render_audit(decoded, self.store)
        self.assertEqual(audit, render_audit(self.message, self.store))
        self.assertEqual(json.loads(audit)["artifacts"][0]["value"], self.store.read(self.ref))

    def test_inline_works_without_store(self):
        inline = Envelope("task-1", "result", "a", "b", {},
                          (attachment(self.ref, self.store.read(self.ref)),))
        self.assertEqual(inline.materialize(ArtifactStore())["artifacts"][0]["value"],
                         self.store.read(self.ref))

    def test_corrupt_inline_rejected(self):
        with self.assertRaises(DigestMismatch):
            attachment(self.ref, {"content": "changed"})

    def test_explicit_null_inline_value_rejected(self):
        with self.assertRaises(ProtocolError):
            attachment(self.ref, None)
        malformed = self.message.to_dict()
        malformed["artifacts"][0]["value"] = None
        with self.assertRaises(ProtocolError):
            Envelope.decode(canonical(malformed))

    def test_malformed_json_is_rejected(self):
        for value in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":"\\ud800"}'):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                parse_json(value)

    def test_malformed_fields_are_protocol_errors(self):
        for key, value in (("op", []), ("op", {}), ("v", True), ("artifacts", {}), ("body", [])):
            malformed = self.message.to_dict()
            malformed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ProtocolError):
                Envelope.decode(json.dumps(malformed))

    def test_unknown_fields_and_versions_rejected(self):
        malformed = self.message.to_dict()
        malformed["surprise"] = True
        with self.assertRaises(ProtocolError):
            Envelope.decode(canonical(malformed))
        malformed = self.message.to_dict()
        malformed["v"] = 2
        with self.assertRaises(ProtocolError):
            Envelope.decode(canonical(malformed))

    def test_mutated_envelope_revalidated(self):
        self.message.artifacts[0]["value"] = {"content": "tampered"}
        with self.assertRaises(DigestMismatch):
            self.message.encode()

    def test_string_keys_and_finite_numbers_required(self):
        for value in ({1: "one"}, {"number": float("inf")}, {"nested": {"not-json"}}):
            with self.assertRaises(ProtocolError):
                canonical(value)


if __name__ == "__main__":
    unittest.main()
