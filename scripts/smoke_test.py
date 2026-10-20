"""End-to-end check of a comfyui-headless install.

Starts the server, checks authentication, frontend removal, a prompt round trip, the
websocket and token revocation, then stops the server.

Usage: python scripts/smoke_test.py [--config PATH] [--timeout SECONDS] [-- COMFYUI_ARGS...]
"""

import argparse
import base64
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from comfyui_headless import config as config_mod, launch  # noqa: E402
from comfyui_headless.guard import HEALTH_PATH, MARKER_HEADER  # noqa: E402

HEADLESS = os.path.join(ROOT, "headless.py")
PROMPT = {
    "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 16744448}},
    "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}},
}


class Client:
    """Minimal HTTP client for one host and port."""

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def request(self, method, path, token=None, header="bearer", body=None):
        """Send a request; return (status, lower-cased headers, body bytes)."""
        headers = {}
        if token and header == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        elif token:
            headers["X-API-Key"] = token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=60)
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            conn.close()


def websocket_first_message(host, port, path, token=None):
    """Open a websocket; return (status, first text frame as JSON or None)."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    sock = socket.create_connection((host, port), timeout=30)
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        if status != 101:
            return status, None

        def take(count):
            nonlocal rest
            while len(rest) < count:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("websocket closed")
                rest += chunk
            data, rest = rest[:count], rest[count:]
            return data

        while True:
            first, second = take(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = int.from_bytes(take(2), "big")
            elif length == 127:
                length = int.from_bytes(take(8), "big")
            payload = take(length)
            if opcode == 1:
                return status, json.loads(payload.decode("utf-8"))
            if opcode == 8:
                return status, None
    finally:
        sock.close()


def cli(config_args, *args):
    """Run headless.py and return stdout."""
    result = subprocess.run([sys.executable, HEADLESS, *config_args, *args], text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"headless.py {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def start_server(config_args, comfy_args):
    """Start `headless.py run` in its own process group."""
    command = [sys.executable, HEADLESS, *config_args, "run"]
    if comfy_args:
        command += ["--", *comfy_args]
    if os.name == "nt":
        return subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(command, start_new_session=True)


def stop_server(proc):
    """Stop the launcher and ComfyUI."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            subprocess.call(["taskkill", "/T", "/F", "/PID", str(proc.pid)])
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="headless.toml to test")
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for startup")
    argv = sys.argv[1:]
    comfy_args = []
    if "--" in argv:
        split = argv.index("--")
        argv, comfy_args = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)
    path = config_mod.find_config(args.config, ROOT)
    cfg = config_mod.load(path, root=ROOT)
    config_args = ["--config", path] if path else []
    host = launch.probe_host(cfg.listen)
    client = Client(host, cfg.port)
    suffix = uuid.uuid4().hex[:6]
    names = [f"smoke-{suffix}", f"smoke-revoke-{suffix}"]
    token = cli(config_args, "token", "create", "--name", names[0], "--raw")
    revoke_token = cli(config_args, "token", "create", "--name", names[1], "--raw")
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)

    proc = start_server(config_args, comfy_args)
    try:
        deadline = time.monotonic() + args.timeout
        ready = False
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                status, headers, _ = client.request("GET", HEALTH_PATH)
                if status == 200 and headers.get(MARKER_HEADER.lower()) == "1":
                    ready = True
                    break
            except OSError:
                pass
            time.sleep(2)
        check("server started and the guard answers /headless/health", ready,
              f"launcher exit {proc.poll()}" if proc.poll() is not None else "timeout")
        if not ready:
            return 1

        status, headers, _ = client.request("GET", "/queue")
        check("no token: 401 with WWW-Authenticate", status == 401 and "bearer" in headers.get("www-authenticate", "").lower(), status)
        status, _, _ = client.request("GET", "/queue", token=token[:-3] + "xyz")
        check("wrong token: 401", status == 401, status)
        status, _, _ = client.request("GET", "/queue", token=token)
        check("Authorization: Bearer: 200", status == 200, status)
        status, _, _ = client.request("GET", "/system_stats", token=token, header="x-api-key")
        check("X-API-Key: 200", status == 200, status)
        if not cfg.allow_query_token:
            status, _, _ = client.request("GET", f"/queue?token={urllib.parse.quote(token)}")
            check("?token= refused when allow_query_token is false", status == 401, status)
        status, _, _ = client.request("GET", "/internal/logs")
        check("/internal sub-application: 401 without token", status == 401, status)
        status, _, _ = client.request("GET", "/", token=token)
        check("frontend /: 404", status == 404, status)
        status, _, _ = client.request("GET", "/extensions", token=token)
        check("frontend /extensions: 404", status == 404, status)

        status, _, body = client.request("GET", "/object_info", token=token)
        nodes = json.loads(body) if status == 200 else {}
        check("/object_info lists EmptyImage and PreviewImage", "EmptyImage" in nodes and "PreviewImage" in nodes,
              status)

        status, _, body = client.request("POST", "/prompt", token=token, body={"prompt": PROMPT})
        prompt_id = json.loads(body).get("prompt_id") if status == 200 else None
        check("POST /prompt accepted", prompt_id is not None, f"{status} {body[:200]!r}")
        entry = None
        if prompt_id:
            for _ in range(120):
                status, _, body = client.request("GET", f"/history/{prompt_id}", token=token)
                history = json.loads(body) if status == 200 else {}
                if prompt_id in history:
                    entry = history[prompt_id]
                    break
                time.sleep(1)
        succeeded = bool(entry) and entry.get("status", {}).get("status_str") == "success"
        check("prompt finished with status success", succeeded, json.dumps(entry)[:300] if entry else "no history")
        if succeeded:
            image = entry["outputs"]["2"]["images"][0]
            query = urllib.parse.urlencode({"filename": image["filename"], "subfolder": image.get("subfolder", ""),
                                            "type": image.get("type", "temp")})
            status, headers, body = client.request("GET", f"/view?{query}", token=token)
            check("/view returns the PNG", status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", status)
            status, _, _ = client.request("GET", f"/view?{query}")
            check("/view without token: 401", status == 401, status)

        status, _ = websocket_first_message(host, cfg.port, "/ws")
        check("websocket without token: 401", status == 401, status)
        status, message = websocket_first_message(host, cfg.port, "/ws", token=token)
        check("websocket with token: 101 and a status message", status == 101 and bool(message) and "type" in message,
              f"{status} {message}")

        status, _, _ = client.request("GET", "/queue", token=revoke_token)
        before = status
        cli(config_args, "token", "revoke", names[1])
        time.sleep(1.5)
        status, _, _ = client.request("GET", "/queue", token=revoke_token)
        check("revoked token stops working on the running server", before == 200 and status == 401,
              f"before {before}, after {status}")
    finally:
        stop_server(proc)
        for name in names:
            try:
                cli(config_args, "token", "revoke", name)
            except RuntimeError:
                pass

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
