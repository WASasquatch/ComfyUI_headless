import contextlib
import io
import os
import tempfile
import tomllib
import unittest

from comfyui_headless import config
from comfyui_headless.compat import TESTED_PIN


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "headless.toml")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_template_matches_defaults(self):
        parsed = tomllib.loads(config.template())
        defaults = {s: {k: v[1] for k, v in keys.items()} for s, keys in config.SCHEMA.items()}
        self.assertEqual(parsed, defaults)
        self.assertEqual(parsed["comfyui"]["ref"], TESTED_PIN)

    def test_defaults_without_file(self):
        cfg = config.load(None, root=self.dir.name)
        self.assertEqual(cfg.ref, TESTED_PIN)
        self.assertEqual(cfg.comfyui_path, os.path.join(self.dir.name, "ComfyUI"))
        self.assertTrue(cfg.auth_enabled)
        self.assertIsNone(cfg.path)

    def test_relative_paths_follow_config_file(self):
        self.write('[comfyui]\npath = "sub/ComfyUI"\n[auth]\ntokens_file = "keys/t.json"\n')
        cfg = config.load(self.path)
        self.assertEqual(cfg.comfyui_path, os.path.join(self.dir.name, "sub", "ComfyUI"))
        self.assertEqual(cfg.tokens_file, os.path.join(self.dir.name, "keys", "t.json"))

    def test_type_errors(self):
        self.write("[server]\nport = \"8188\"\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_port_range(self):
        self.write("[server]\nport = 70000\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_public_paths_are_exact(self):
        self.write('[auth]\npublic_paths = ["/view*"]\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_unknown_keys_warn(self):
        self.write("[auth]\nenable = false\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            cfg = config.load(self.path)
        self.assertTrue(cfg.auth_enabled)
        self.assertIn("unknown key [auth] enable", stderr.getvalue())

    def test_find_config_prefers_explicit(self):
        self.write("")
        self.assertEqual(config.find_config(self.path), os.path.abspath(self.path))
        with self.assertRaises(config.ConfigError):
            config.find_config(os.path.join(self.dir.name, "missing.toml"))

    def test_bad_toml(self):
        self.write("[server\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)


if __name__ == "__main__":
    unittest.main()
