"""ComfyUI checkout at the configured ref and its Python environment."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from . import __version__, compat
from .config import venv_python

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
FRONTEND_PACKAGE = "comfyui-frontend-package"


def normal_name(name: str) -> str:
    """Return a package name normalised per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


class InstallError(Exception):
    """Raised when an install step fails; the message names the fix."""


def _say(message):
    """Print an install line to stderr."""
    sys.stderr.write(f"comfyui-headless: {message}\n")
    sys.stderr.flush()


def _git(args, capture=False):
    """Run git.

    Returns:
        Stripped stdout when capture is True, else None.
    Raises:
        InstallError: when git is missing or exits non-zero.
    """
    try:
        result = subprocess.run(["git", *args], check=True, text=True,
                                capture_output=capture)
    except FileNotFoundError:
        raise InstallError("git was not found. Install git and run install again.") from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise InstallError(f"git {' '.join(args)} failed with status {exc.returncode}."
                           + (f"\n{detail}" if detail else "")) from None
    return result.stdout.strip() if capture else None


def _resolve(path, ref):
    """Return the commit SHA ref names in the checkout at path, or None."""
    result = subprocess.run(["git", "-C", path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            text=True, capture_output=True)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and _SHA_RE.match(sha) else None


def checkout(cfg, ref: str, force: bool = False) -> str:
    """Clone or update [comfyui] path and check out ref detached.

    Args:
        cfg: Config.
        ref: Commit, tag or branch.
        force: Check out over local changes.
    Returns:
        Checked-out commit SHA.
    Raises:
        InstallError: on a non-git folder, local changes, an unknown ref or a git failure.
    """
    path = cfg.comfyui_path
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _say(f"cloning {cfg.repo} into {path}")
        _git(["clone", "--filter=blob:none", "--no-checkout", cfg.repo, path])
    elif not os.path.exists(os.path.join(path, ".git")):
        raise InstallError(f"{path} exists and is not a git checkout. Move it, or set [comfyui] manage = false "
                           "to run it as it is.")
    else:
        changes = _git(["-C", path, "status", "--porcelain", "--untracked-files=no"], capture=True)
        if changes and not force:
            raise InstallError(f"{path} has local changes:\n{changes}\n"
                               "Commit or discard them, or run install --force to check out over them.")
    if _SHA_RE.match(ref):
        sha = _resolve(path, ref)
        if sha is None:
            _say(f"fetching {ref[:12]}")
            _git(["-C", path, "fetch", "origin", ref])
            sha = _resolve(path, ref)
    else:
        try:
            _say(f"fetching {ref}")
            _git(["-C", path, "fetch", "--tags", "origin", ref])
            sha = _resolve(path, "FETCH_HEAD")
        except InstallError:
            sha = _resolve(path, ref)
    if sha is None:
        raise InstallError(f"{ref} was not found in {cfg.repo}. Set [comfyui] ref to a commit, tag or branch.")
    _git(["-C", path, "checkout", "--quiet", "--detach", *(["--force"] if force else []), sha])
    return sha


def filtered_requirements(comfy_dir: str, skip: list) -> list:
    """Return ComfyUI's requirements.txt lines without the skipped packages.

    Args:
        comfy_dir: ComfyUI root.
        skip: Package names to drop; compared after PEP 503 normalisation.
    Returns:
        List of requirement lines.
    Raises:
        InstallError: when requirements.txt is missing.
    """
    dropped = {normal_name(name) for name in skip}
    try:
        with open(os.path.join(comfy_dir, "requirements.txt"), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise InstallError(f"Cannot read {comfy_dir}/requirements.txt: {exc}") from None
    kept = []
    for line in lines:
        match = _NAME_RE.match(line)
        if match and not line.lstrip().startswith("-") and normal_name(match.group(1)) in dropped:
            continue
        kept.append(line)
    return kept


def _run(command):
    """Run a command with inherited output.

    Raises:
        InstallError: on a non-zero exit.
    """
    _say("running " + " ".join(command))
    code = subprocess.call(command)
    if code != 0:
        raise InstallError(f"{command[0]} exited with status {code}.")


def prepare_python(cfg, report) -> str:
    """Create the venv when needed and install torch and ComfyUI's requirements.

    Args:
        cfg: Config.
        report: compat.Report; without the frontend root flag the frontend package is kept.
    Returns:
        Interpreter path.
    Raises:
        InstallError: on a missing interpreter, an unsupported version or a pip failure.
    """
    if cfg.python_executable:
        python = cfg.python()
        if python is None:
            raise InstallError(f"[python] executable {cfg.python_executable} was not found.")
    else:
        python = venv_python(cfg.venv_path)
        if not os.path.isfile(python):
            _say(f"creating virtual environment {cfg.venv_path}")
            _run([sys.executable, "-m", "venv", cfg.venv_path])
    probe = subprocess.run([python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                           text=True, capture_output=True)
    version = probe.stdout.strip()
    if probe.returncode != 0 or tuple(int(p) for p in version.split(".")) < (3, 10):
        raise InstallError(f"{python} is Python {version or 'unknown'}; ComfyUI needs 3.10 or newer.")
    pip = [python, *cfg.python_flags, "-m", "pip", "install"]
    if cfg.install_torch and cfg.torch_packages:
        command = pip + list(cfg.torch_packages)
        if cfg.torch_index_url:
            command += ["--index-url", cfg.torch_index_url]
        _run(command)
    skip = list(cfg.skip_packages)
    if not report.has("front_end_root"):
        skip = [name for name in skip if normal_name(name) != FRONTEND_PACKAGE]
    requirements = os.path.join(cfg.state_dir, "requirements.txt")
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(requirements, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(filtered_requirements(cfg.comfyui_path, skip)) + "\n")
    _run(pip + ["-r", requirements])
    return python


def write_state(cfg, commit, python):
    """Record the installed commit and interpreter in <state>/state.json."""
    os.makedirs(cfg.state_dir, exist_ok=True)
    state = {
        "harness_version": __version__,
        "commit": commit,
        "ref": cfg.ref,
        "python": python,
        "installed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(cfg.state_dir, "state.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")


def install(cfg, ref: str | None = None, force: bool = False, python_deps: bool = True) -> int:
    """Check out ComfyUI, verify compatibility and prepare Python.

    Args:
        cfg: Config.
        ref: Overrides [comfyui] ref.
        force: Check out over local changes.
        python_deps: Install torch and requirements.
    Returns:
        0, or EXIT_INCOMPATIBLE.
    Raises:
        InstallError: on a failed step.
    """
    ref = (ref or cfg.ref).strip()
    if cfg.manage:
        commit = checkout(cfg, ref, force=force)
    else:
        if not os.path.isfile(os.path.join(cfg.comfyui_path, "main.py")):
            raise InstallError(f"[comfyui] manage = false and no ComfyUI exists at {cfg.comfyui_path}.")
        _say(f"[comfyui] manage = false: leaving {cfg.comfyui_path} as it is")
        commit = compat.read_commit(cfg.comfyui_path)
    report = compat.check(cfg.comfyui_path)
    if not report.compatible:
        _say(compat.incompatible_message(report))
        return compat.EXIT_INCOMPATIBLE
    if not report.tested:
        _say(f"warning: ComfyUI {report.label()} is not a tested commit; "
             f"the tested pin is {compat.TESTED_PIN[:12]}.")
    python = prepare_python(cfg, report) if python_deps else cfg.python()
    write_state(cfg, commit, python)
    _say(f"installed ComfyUI {report.label()} at {cfg.comfyui_path}")
    if python_deps:
        _say("next: `comfyui-headless token create`, then `comfyui-headless run`")
    return 0
