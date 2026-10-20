"""Headless launch: ComfyUI arguments, the child process and the startup self-test."""

import http.client
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

from . import __version__, bootstrap, compat, tokens
from .guard import HEALTH_PATH, MARKER_HEADER, SELF_TEST_PATH

EXIT_SELF_TEST = 70
MANAGED_FLAGS = ("--listen", "--port", "--front-end-root", "--front-end-version")


class LaunchError(Exception):
    """Raised when the launch cannot proceed; the message names the fix."""


def _say(message):
    """Print a launcher line to stderr."""
    sys.stderr.write(f"comfyui-headless: {message}\n")
    sys.stderr.flush()


def flag_value(args: list, flag: str, const: str | None = None) -> str | None:
    """Return the value given to flag in args.

    Args:
        args: Argument list.
        flag: Long option, e.g. --enable-cors-header.
        const: Value for a bare flag with no argument.
    Returns:
        The value, const for a bare flag, or None when absent.
    """
    value = None
    for index, arg in enumerate(args):
        if arg == flag:
            following = args[index + 1] if index + 1 < len(args) else None
            value = following if following is not None and not following.startswith("-") else const
        elif arg.startswith(flag + "="):
            value = arg.split("=", 1)[1]
    return value


def comfy_arguments(cfg, report, extra: list, web_root: str) -> list:
    """Build ComfyUI's argument list.

    Args:
        cfg: Config.
        report: compat.Report for cfg.comfyui_path.
        extra: Arguments given after `--`.
        web_root: Empty folder for --front-end-root.
    Returns:
        List of str.
    Raises:
        LaunchError: when [server] args or extra set a managed flag.
    """
    user = list(cfg.server_args) + list(extra)
    for arg in user:
        flag = arg.split("=", 1)[0]
        if flag in ("--listen", "--port"):
            raise LaunchError(f"{flag} is set from [server] in {cfg.describe()}; remove it from the arguments.")
        if flag in MANAGED_FLAGS:
            raise LaunchError(f"{flag} is managed by comfyui-headless and cannot be passed.")
    args = ["--listen", cfg.listen, "--port", str(cfg.port)]
    if report.has("front_end_root"):
        args += ["--front-end-root", web_root]
    if report.has("auto_launch"):
        args.append("--disable-auto-launch")
    return args + user


def build_runtime(cfg, args: list) -> dict:
    """Return the settings the guard reads inside the ComfyUI process."""
    return {
        "version": __version__,
        "comfyui_path": cfg.comfyui_path,
        "auth": {
            "enabled": cfg.auth_enabled,
            "tokens_file": cfg.tokens_file,
            "allow_query_token": cfg.allow_query_token,
            "public_paths": list(cfg.public_paths),
        },
        "headless": {"block_static": cfg.block_static, "block_paths": list(cfg.block_paths)},
        "cors_origin": flag_value(args, "--enable-cors-header", const="*"),
    }


def probe_host(listen: str) -> str:
    """Return the address the self-test connects to for a --listen value."""
    first = listen.split(",")[0].strip()
    if first in ("", "0.0.0.0"):
        return "127.0.0.1"
    if first == "::":
        return "::1"
    return first.strip("[]")


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True when a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _request(host, port, tls, path):
    """GET path without credentials; return (status, marker header value)."""
    if tls:
        conn = http.client.HTTPSConnection(host, port, timeout=15, context=ssl._create_unverified_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        return response.status, response.getheader(MARKER_HEADER)
    finally:
        conn.close()


def self_test(host: str, port: int, tls: bool, auth_enabled: bool, proc) -> tuple | None:
    """Wait for the server to listen, then confirm the guard answers.

    Args:
        host: Address to connect to.
        port: Port.
        tls: True for HTTPS.
        auth_enabled: True to require 401 on an unauthenticated request.
        proc: ComfyUI child process.
    Returns:
        (ok, message), or None when the child exited before listening.
    """
    while not port_open(host, port):
        if proc.poll() is not None:
            return None
        time.sleep(1.0)
    error = None
    for _ in range(5):
        try:
            status, marker = _request(host, port, tls, HEALTH_PATH)
            if marker != "1":
                return False, f"{HEALTH_PATH} answered {status} without the guard header"
            if auth_enabled:
                status, marker = _request(host, port, tls, SELF_TEST_PATH)
                if status != 401 or marker != "1":
                    return False, f"an unauthenticated GET {SELF_TEST_PATH} returned {status}"
            return True, ""
        except (OSError, http.client.HTTPException) as exc:
            error = exc
            time.sleep(1.0)
    return False, f"the self-test request failed ({error})"


def _write_runtime(path, runtime):
    """Write runtime settings as JSON with LF line endings."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(runtime, handle, indent=2)
        handle.write("\n")


def preflight(cfg):
    """Check the install and tokens before launching.

    Returns:
        (report, python) on success, or (report, None) when ComfyUI is incompatible.
    Raises:
        LaunchError: on a missing install, interpreter or unreadable token file.
    """
    if not os.path.isfile(os.path.join(cfg.comfyui_path, "main.py")):
        raise LaunchError(f"No ComfyUI at {cfg.comfyui_path}. Run `comfyui-headless install`, "
                          f"or set [comfyui] path in {cfg.describe()}.")
    report = compat.check(cfg.comfyui_path)
    if not report.compatible:
        _say(compat.incompatible_message(report))
        return report, None
    if not report.tested:
        _say(f"warning: ComfyUI {report.label()} is not a tested commit; "
             f"the tested pin is {compat.TESTED_PIN[:12]}. Startup checks still apply.")
    if cfg.manage and report.commit and len(cfg.ref) == 40 and report.commit != cfg.ref.lower():
        _say(f"warning: [comfyui] ref is {cfg.ref[:12]} but the checkout is at {report.commit[:12]}. "
             "Run `comfyui-headless install` to switch.")
    python = cfg.python()
    if python is None:
        target = cfg.python_executable or cfg.venv_path
        raise LaunchError(f"Python for ComfyUI not found ({target}). Run `comfyui-headless install`, "
                          f"or set [python] executable in {cfg.describe()}.")
    if cfg.auth_enabled:
        try:
            records = tokens.load(cfg.tokens_file)
        except tokens.TokenStoreError as exc:
            raise LaunchError(str(exc)) from None
        if not any(not r.expired() for r in records):
            _say(f"warning: no active tokens in {cfg.tokens_file}; every request is rejected. "
                 "Create one with `comfyui-headless token create`.")
    else:
        _say("warning: token authentication is OFF ([auth] enabled = false).")
    return report, python


def run(cfg, extra: list) -> int:
    """Launch ComfyUI headless and wait for it to exit.

    Args:
        cfg: Config.
        extra: ComfyUI arguments given after `--`.
    Returns:
        Exit status: ComfyUI's own, EXIT_INCOMPATIBLE or EXIT_SELF_TEST.
    Raises:
        LaunchError: on a setup problem.
    """
    report, python = preflight(cfg)
    if python is None:
        return compat.EXIT_INCOMPATIBLE
    host = probe_host(cfg.listen)
    if port_open(host, cfg.port):
        raise LaunchError(f"Port {cfg.port} on {host} is already in use. Stop that server or set [server] port.")
    web_root = os.path.join(cfg.state_dir, "web")
    os.makedirs(web_root, exist_ok=True)
    args = comfy_arguments(cfg, report, extra, web_root)
    runtime = build_runtime(cfg, args)
    runtime_path = os.path.join(cfg.state_dir, "runtime.json")
    _write_runtime(runtime_path, runtime)
    tls = bool(flag_value(args, "--tls-keyfile") and flag_value(args, "--tls-certfile"))
    package_dir = os.path.dirname(os.path.abspath(__file__))
    command = [python, *cfg.python_flags, "-c", bootstrap.command(package_dir), runtime_path, *args]
    _say(f"starting ComfyUI {report.label()} from {cfg.comfyui_path}")
    proc = subprocess.Popen(command, cwd=cfg.comfyui_path)
    outcome = {}

    def verify():
        result = self_test(host, cfg.port, tls, cfg.auth_enabled, proc)
        if result is None:
            return
        ok, message = result
        outcome["ok"] = ok
        if ok:
            scheme = "https" if tls else "http"
            shown = f"[{host}]" if ":" in host else host
            auth = "token authentication on" if cfg.auth_enabled else "token authentication OFF"
            _say(f"API ready at {scheme}://{shown}:{cfg.port} ({auth}, frontend removed)")
        else:
            _say(f"self-test failed: {message}. Stopping ComfyUI.")
            proc.terminate()

    threading.Thread(target=verify, daemon=True).start()
    if hasattr(signal, "SIGTERM") and os.name != "nt":
        signal.signal(signal.SIGTERM, lambda signum, frame: proc.terminate())
    code = _wait(proc)
    if outcome.get("ok") is False:
        return EXIT_SELF_TEST
    if code == compat.EXIT_INCOMPATIBLE:
        _say("ComfyUI stopped: incompatible version (see the message above).")
    return code


def _wait(proc) -> int:
    """Wait for the child, giving it 30 s to stop after Ctrl+C before killing it."""
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            return proc.wait(timeout=30)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            return proc.wait()
