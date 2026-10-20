import os
import tempfile
import unittest

from comfyui_headless import compat

SHA = "0123456789abcdef0123456789abcdef01234567"

FILES = {
    "main.py": "import comfy.options\ncomfy.options.enable_args_parsing()\nimport server\ndef start_comfyui():\n    pass\n",
    "server.py": ("class PromptServer:\n    def __init__(self):\n        self.app = web.Application(middlewares=[])\n"
                  "    def add_routes(self):\n        pass\n    async def start_multi_address(self):\n        pass\n"),
    "comfy/cli_args.py": ('parser.add_argument("--listen")\nparser.add_argument("--port")\n'
                          'parser.add_argument("--front-end-root")\nparser.add_argument("--disable-auto-launch")\n'),
    "comfyui_version.py": '__version__ = "0.42.0"\n',
}


class CompatTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        for rel, text in FILES.items():
            self.put(rel, text)

    def tearDown(self):
        self.dir.cleanup()

    def put(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_compatible_tree(self):
        report = compat.check(self.root)
        self.assertTrue(report.compatible)
        self.assertFalse(report.failed)
        self.assertEqual(report.version, "0.42.0")
        self.assertTrue(report.has("front_end_root"))

    def test_missing_required_is_incompatible(self):
        self.put("server.py", "class PromptServer:\n    pass\n")
        report = compat.check(self.root)
        self.assertFalse(report.compatible)
        message = compat.incompatible_message(report)
        self.assertIn("The version of ComfyUI is incompatible", message)
        self.assertIn("server application", message)
        self.assertIn(compat.TESTED_PIN, message)

    def test_missing_optional_stays_compatible(self):
        self.put("comfy/cli_args.py", 'parser.add_argument("--listen")\nparser.add_argument("--port")\n')
        report = compat.check(self.root)
        self.assertTrue(report.compatible)
        self.assertFalse(report.has("front_end_root"))
        self.assertFalse(report.has("auto_launch"))

    def test_empty_folder(self):
        with tempfile.TemporaryDirectory() as empty:
            report = compat.check(empty)
            self.assertFalse(report.compatible)
            self.assertIsNone(report.commit)

    def test_commit_from_branch_ref(self):
        self.put(".git/HEAD", "ref: refs/heads/master\n")
        self.put(".git/refs/heads/master", SHA + "\n")
        self.assertEqual(compat.read_commit(self.root), SHA)

    def test_commit_from_packed_refs(self):
        self.put(".git/HEAD", "ref: refs/heads/master\n")
        self.put(".git/packed-refs", f"# pack-refs with: peeled\n{SHA} refs/heads/master\n")
        self.assertEqual(compat.read_commit(self.root), SHA)

    def test_detached_head(self):
        self.put(".git/HEAD", SHA + "\n")
        report = compat.check(self.root)
        self.assertEqual(report.commit, SHA)
        self.assertFalse(report.tested)

    def test_tested_pin(self):
        self.put(".git/HEAD", compat.TESTED_PIN + "\n")
        self.assertTrue(compat.check(self.root).tested)


if __name__ == "__main__":
    unittest.main()
