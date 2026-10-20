import os
import tempfile
import unittest

from comfyui_headless import config, launch
from comfyui_headless.compat import PROBES, Report


def report(*keys):
    result = Report(path="x", commit=None, version=None)
    for probe in PROBES:
        (result.passed if probe.key in keys else result.failed).append(probe)
    return result


class LaunchArgumentsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = config.load(None, root=self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_managed_flags(self):
        args = launch.comfy_arguments(self.cfg, report("front_end_root", "auto_launch"), ["--lowvram"], "/w")
        self.assertEqual(args[:4], ["--listen", "127.0.0.1", "--port", "8188"])
        self.assertIn("--front-end-root", args)
        self.assertEqual(args[args.index("--front-end-root") + 1], "/w")
        self.assertIn("--disable-auto-launch", args)
        self.assertEqual(args[-1], "--lowvram")

    def test_optional_flags_dropped_when_missing(self):
        args = launch.comfy_arguments(self.cfg, report(), [], "/w")
        self.assertNotIn("--front-end-root", args)
        self.assertNotIn("--disable-auto-launch", args)

    def test_rejects_overrides(self):
        for bad in (["--listen", "0.0.0.0"], ["--port=1"], ["--front-end-root", "x"], ["--front-end-version", "a@b"]):
            with self.assertRaises(launch.LaunchError):
                launch.comfy_arguments(self.cfg, report(), bad, "/w")

    def test_flag_value(self):
        self.assertEqual(launch.flag_value(["--enable-cors-header"], "--enable-cors-header", const="*"), "*")
        self.assertEqual(launch.flag_value(["--enable-cors-header", "https://a.b"], "--enable-cors-header"), "https://a.b")
        self.assertEqual(launch.flag_value(["--enable-cors-header=https://c"], "--enable-cors-header"), "https://c")
        self.assertIsNone(launch.flag_value(["--cpu"], "--enable-cors-header"))

    def test_runtime_carries_cors(self):
        runtime = launch.build_runtime(self.cfg, ["--enable-cors-header", "--cpu"])
        self.assertEqual(runtime["cors_origin"], "*")
        self.assertEqual(runtime["auth"]["tokens_file"], os.path.join(self.dir.name, "tokens.json"))

    def test_probe_host(self):
        self.assertEqual(launch.probe_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(launch.probe_host("0.0.0.0,::"), "127.0.0.1")
        self.assertEqual(launch.probe_host("::"), "::1")
        self.assertEqual(launch.probe_host("10.0.0.5"), "10.0.0.5")


if __name__ == "__main__":
    unittest.main()
