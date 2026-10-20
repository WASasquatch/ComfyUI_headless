"""ComfyUI compatibility probes, tested commits and commit detection."""

import os
import re
from dataclasses import dataclass, field

from . import __version__

TESTED_COMMITS = (
    {"commit": "8f04fd6ecf4a548937971e840af2a985529fab76", "version": "0.42.0", "date": "2026-10-19"},
)
TESTED_PIN = TESTED_COMMITS[0]["commit"]

EXIT_INCOMPATIBLE = 78

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


@dataclass(frozen=True)
class Probe:
    """Text probe: every needle must appear in one ComfyUI source file.

    Attributes:
        key: Identifier used by the launcher.
        name: Label printed in reports.
        path: File relative to the ComfyUI root, forward slashes.
        needles: Strings the file must contain.
        required: True when a miss makes the install incompatible.
    """

    key: str
    name: str
    path: str
    needles: tuple
    required: bool


PROBES = (
    Probe("entry", "entry point", "main.py",
          ("comfy.options.enable_args_parsing", "import server", "def start_comfyui"), True),
    Probe("server_app", "server application", "server.py",
          ("class PromptServer", "self.app = web.Application(", "def add_routes"), True),
    Probe("listen", "listen and port flags", "comfy/cli_args.py", ('"--listen"', '"--port"'), True),
    Probe("bind", "bind hook", "server.py", ("async def start_multi_address",), False),
    Probe("front_end_root", "frontend root flag", "comfy/cli_args.py", ('"--front-end-root"',), False),
    Probe("auto_launch", "auto-launch flag", "comfy/cli_args.py", ('"--disable-auto-launch"',), False),
)


@dataclass
class Report:
    """Result of probing a ComfyUI tree.

    Attributes:
        path: ComfyUI root.
        commit: Checked-out commit, or None.
        version: comfyui_version.__version__, or None.
        passed: Probes that matched.
        failed: Probes that did not match.
    """

    path: str
    commit: str | None
    version: str | None
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        """True when every required probe matched."""
        return not any(p.required for p in self.failed)

    @property
    def tested(self) -> bool:
        """True when the commit is in TESTED_COMMITS."""
        return self.commit is not None and any(t["commit"] == self.commit for t in TESTED_COMMITS)

    def has(self, key: str) -> bool:
        """Return True when the probe with this key matched."""
        return any(p.key == key for p in self.passed)

    def label(self) -> str:
        """Short commit and version, e.g. '8f04fd6ecf4a (0.42.0)'."""
        text = self.commit[:12] if self.commit else "unknown commit"
        return f"{text} ({self.version})" if self.version else text


def _read(path):
    """Return a file's text, or None when it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def check(path: str) -> Report:
    """Probe a ComfyUI tree.

    Args:
        path: ComfyUI root.
    Returns:
        Report.
    """
    report = Report(path=path, commit=read_commit(path), version=read_version(path))
    texts = {}
    for probe in PROBES:
        if probe.path not in texts:
            texts[probe.path] = _read(os.path.join(path, *probe.path.split("/")))
        text = texts[probe.path]
        matched = text is not None and all(needle in text for needle in probe.needles)
        (report.passed if matched else report.failed).append(probe)
    return report


def incompatible_text(location: str, reason: str) -> str:
    """Build the incompatibility message.

    Args:
        location: ComfyUI path and commit label.
        reason: What is missing.
    Returns:
        Message naming the tested pin and the fix.
    """
    return (
        f"The version of ComfyUI is incompatible: {location} is missing {reason}. "
        f"comfyui-headless {__version__} is tested on ComfyUI {TESTED_PIN[:12]}. "
        f'Set [comfyui] ref = "{TESTED_PIN}" and run install, '
        f"or point [comfyui] path at a compatible ComfyUI."
    )


def incompatible_message(report: Report) -> str:
    """Build the incompatibility message for a failed report.

    Args:
        report: Report with at least one failed required probe.
    Returns:
        Message text.
    """
    missing = ", ".join(p.name for p in report.failed if p.required)
    return incompatible_text(f"{report.path} at {report.label()}", missing)


def read_version(path: str) -> str | None:
    """Return comfyui_version.__version__ from a ComfyUI root, or None."""
    text = _read(os.path.join(path, "comfyui_version.py"))
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else None


def read_commit(path: str) -> str | None:
    """Return the checked-out commit of a git work tree without running git.

    Args:
        path: Work tree root.
    Returns:
        40-character hex SHA, or None.
    """
    git_dir = os.path.join(path, ".git")
    if os.path.isfile(git_dir):
        match = re.match(r"gitdir:\s*(.+)", (_read(git_dir) or "").strip())
        if not match:
            return None
        git_dir = match.group(1).strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.normpath(os.path.join(path, git_dir))
    if not os.path.isdir(git_dir):
        return None
    head = (_read(os.path.join(git_dir, "HEAD")) or "").strip()
    if _SHA_RE.match(head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head[4:].strip()
    common = git_dir
    common_file = _read(os.path.join(git_dir, "commondir"))
    if common_file:
        common = os.path.normpath(os.path.join(git_dir, common_file.strip()))
    for base in (git_dir, common):
        value = (_read(os.path.join(base, *ref.split("/"))) or "").strip()
        if _SHA_RE.match(value):
            return value
    for line in (_read(os.path.join(common, "packed-refs")) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref and _SHA_RE.match(parts[0]):
            return parts[0]
    return None
