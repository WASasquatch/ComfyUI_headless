"""Run comfyui-headless from a checkout without installing it."""

import os
import sys

if sys.version_info < (3, 11):
    sys.stderr.write("comfyui-headless: Python 3.11 or newer is required; this is %d.%d.\n" % sys.version_info[:2])
    sys.exit(1)

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if os.name == "nt" and SRC.startswith("\\\\"):
    SRC = SRC.replace("\\", "/")
sys.path.insert(0, SRC)

from comfyui_headless.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(project_root=ROOT))
