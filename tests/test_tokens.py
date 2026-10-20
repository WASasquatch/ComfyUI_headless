import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from comfyui_headless import tokens


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "tokens.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_create_and_verify(self):
        token, record = tokens.create(self.path, name="ci")
        self.assertTrue(token.startswith(tokens.PREFIX + record.id + "_"))
        self.assertEqual(tokens.split_token(token), record.id)
        verifier = tokens.Verifier(self.path)
        self.assertEqual(verifier.verify(token), record)

    def test_store_holds_hash_only(self):
        token, record = tokens.create(self.path, name="ci")
        with open(self.path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn(token, text)
        self.assertNotIn(token.split("_", 2)[2], text)
        self.assertEqual(json.loads(text)["tokens"][0]["sha256"], tokens.hash_token(token))

    def test_rejects_wrong_and_malformed(self):
        token, _ = tokens.create(self.path, name="ci")
        verifier = tokens.Verifier(self.path)
        self.assertIsNone(verifier.verify(token[:-1] + ("A" if token[-1] != "A" else "B")))
        self.assertIsNone(verifier.verify("chl_zzzzzzzz_abc"))
        self.assertIsNone(verifier.verify(""))
        self.assertIsNone(verifier.verify(None))
        self.assertIsNone(verifier.verify("x" * 5000))

    def test_duplicate_and_invalid_names(self):
        tokens.create(self.path, name="ci")
        with self.assertRaises(tokens.TokenStoreError):
            tokens.create(self.path, name="ci")
        with self.assertRaises(tokens.TokenStoreError):
            tokens.create(self.path, name="bad name")

    def test_default_name(self):
        _, record = tokens.create(self.path)
        self.assertEqual(record.name, f"token-{record.id}")

    def test_revoke_by_name_and_id(self):
        _, first = tokens.create(self.path, name="a")
        _, second = tokens.create(self.path, name="b")
        self.assertEqual(tokens.revoke(self.path, "a"), first)
        self.assertEqual(tokens.revoke(self.path, second.id), second)
        self.assertEqual(tokens.load(self.path), [])
        with self.assertRaises(tokens.TokenStoreError):
            tokens.revoke(self.path, "a")

    def test_verifier_reloads_after_revoke(self):
        token, _ = tokens.create(self.path, name="ci")
        verifier = tokens.Verifier(self.path, min_interval=0)
        self.assertIsNotNone(verifier.verify(token))
        tokens.revoke(self.path, "ci")
        self.assertIsNone(verifier.verify(token))

    def test_verifier_sees_new_tokens(self):
        verifier = tokens.Verifier(self.path, min_interval=0)
        self.assertEqual(verifier.count, 0)
        token, _ = tokens.create(self.path, name="later")
        self.assertIsNotNone(verifier.verify(token))

    def test_expiry(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        token, record = tokens.create(self.path, name="short", expires=timedelta(hours=1), now=now)
        verifier = tokens.Verifier(self.path)
        self.assertIsNotNone(verifier.verify(token, now=now + timedelta(minutes=59)))
        self.assertIsNone(verifier.verify(token, now=now + timedelta(hours=1)))
        self.assertTrue(record.expired(now + timedelta(hours=2)))

    def test_corrupt_store_rejects_everything(self):
        token, _ = tokens.create(self.path, name="ci")
        errors = []
        verifier = tokens.Verifier(self.path, min_interval=0, on_error=errors.append)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(verifier.verify(token))
        self.assertEqual(len(errors), 1)
        with self.assertRaises(tokens.TokenStoreError):
            tokens.load(self.path)

    def test_malformed_entry(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "tokens": [{"id": "nothex!!", "name": "x", "sha256": "0", "created": "x"}]}, handle)
        with self.assertRaises(tokens.TokenStoreError):
            tokens.load(self.path)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_owner_only_permissions(self):
        tokens.create(self.path, name="ci")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_parse_duration(self):
        self.assertEqual(tokens.parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(tokens.parse_duration("12h"), timedelta(hours=12))
        self.assertEqual(tokens.parse_duration("30d"), timedelta(days=30))
        self.assertEqual(tokens.parse_duration("2w"), timedelta(weeks=2))
        self.assertIsNone(tokens.parse_duration("never"))
        for bad in ("0d", "5", "1y", "-1d"):
            with self.assertRaises(ValueError):
                tokens.parse_duration(bad)


if __name__ == "__main__":
    unittest.main()
