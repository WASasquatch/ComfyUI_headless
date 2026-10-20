"""In-process ComfyUI hooks: token authentication middleware and frontend removal."""

import functools
import importlib.abc
import importlib.util
import inspect
import logging
import os
import sys
import time

from .compat import EXIT_INCOMPATIBLE, incompatible_text
from .tokens import Verifier

LOG = logging.getLogger("comfyui_headless")
MARKER_HEADER = "X-ComfyUI-Headless"
HEALTH_PATH = "/headless/health"
SELF_TEST_PATH = "/headless/self-test"
SERVER_MODULE = "server"
TEMPLATES_PACKAGE = "comfyui_workflow_templates"
_TEMPLATE_LOOKUPS = ("legacy_templates_path", "template_asset_handler", "template_asset_map")
_ATTR = "_comfyui_headless_middleware"
_CORS_METHODS = "POST, GET, DELETE, PUT, OPTIONS, PATCH"
_CORS_HEADERS = "Content-Type, Authorization, X-API-Key"

_state = {"runtime": None}


def incompatible(reason: str):
    """Print the incompatibility message and exit.

    Args:
        reason: What the running ComfyUI is missing.
    Raises:
        SystemExit: always, with EXIT_INCOMPATIBLE.
    """
    location = (_state["runtime"] or {}).get("comfyui_path", "ComfyUI")
    sys.stderr.write(f"\ncomfyui-headless: {incompatible_text(location, reason)}\n\n")
    sys.stderr.flush()
    raise SystemExit(EXIT_INCOMPATIBLE)


class _AfterImport(importlib.abc.MetaPathFinder):
    """Meta path finder that runs a callback once a named module has executed."""

    def __init__(self, name, callback):
        self.name = name
        self.callback = callback
        self._searching = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.name or self._searching:
            return None
        self._searching = True
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            self._searching = False
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        original = spec.loader.exec_module
        finder = self

        def exec_module(module):
            original(module)
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
            finder.callback(module)

        spec.loader.exec_module = exec_module
        return spec


def after_import(name: str, callback):
    """Run callback(module) right after the top-level module name first executes.

    Args:
        name: Top-level module name.
        callback: Called with the executed module.
    Returns:
        The installed finder.
    """
    finder = _AfterImport(name, callback)
    sys.meta_path.insert(0, finder)
    return finder


def install(runtime: dict):
    """Register the hook that patches ComfyUI's server module on import.

    Args:
        runtime: Settings written by the launcher.
    Raises:
        SystemExit: when the server module is already imported.
    """
    _state["runtime"] = runtime
    if SERVER_MODULE in sys.modules:
        incompatible("a server module that was imported before the guard could attach")
    after_import(SERVER_MODULE, _patch_server)


def _same_file(a, b):
    """Return True when two paths name the same file."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _patch_server(module):
    """Wrap PromptServer.__init__ and start_multi_address with the guard."""
    runtime = _state["runtime"]
    origin = getattr(module, "__file__", None) or ""
    expected = os.path.join(runtime["comfyui_path"], "server.py")
    if not origin or not _same_file(origin, expected):
        incompatible(f"its own server.py (the server module resolved to {origin or 'nothing'})")
    cls = getattr(module, "PromptServer", None)
    if not isinstance(cls, type):
        incompatible("server.PromptServer")
    original_init = cls.__init__

    @functools.wraps(original_init)
    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        _attach(self)

    cls.__init__ = __init__
    start = getattr(cls, "start_multi_address", None)
    if inspect.iscoroutinefunction(start):
        @functools.wraps(start)
        async def start_multi_address(self, *args, **kwargs):
            _ensure_first(self)
            return await start(self, *args, **kwargs)

        cls.start_multi_address = start_multi_address
    _quiet_missing_templates(module)


def _quiet_missing_templates(module):
    """Make FrontendManager's template lookups return None when comfyui-workflow-templates is absent."""
    manager = getattr(module, "FrontendManager", None)
    if not isinstance(manager, type) or importlib.util.find_spec(TEMPLATES_PACKAGE) is not None:
        return
    for name in _TEMPLATE_LOOKUPS:
        if isinstance(inspect.getattr_static(manager, name, None), classmethod):
            setattr(manager, name, classmethod(lambda cls: None))


def _attach(server):
    """Insert the guard middleware at the front of PromptServer.app."""
    from aiohttp import web

    app = getattr(server, "app", None)
    if not isinstance(app, web.Application):
        incompatible("an aiohttp application on PromptServer.app")
    if app.frozen:
        incompatible("an unfrozen PromptServer.app after construction")
    middleware = build_middleware(_state["runtime"])
    try:
        app.middlewares.insert(0, middleware)
    except (AttributeError, RuntimeError, TypeError) as exc:
        incompatible(f"a writable aiohttp middleware list ({exc})")
    setattr(server, _ATTR, middleware)
    auth = _state["runtime"]["auth"]
    if auth["enabled"]:
        LOG.info("comfyui-headless: token authentication on, %d token(s) in %s",
                 middleware.verifier.count, auth["tokens_file"])
    else:
        LOG.warning("comfyui-headless: token authentication is OFF ([auth] enabled = false)")


def _ensure_first(server):
    """Check before binding that the guard middleware runs first."""
    middleware = getattr(server, _ATTR, None)
    chain = server.app.middlewares
    if middleware is None or middleware not in chain:
        incompatible("the comfyui-headless middleware at bind time")
    if chain[0] is not middleware:
        if server.app.frozen:
            incompatible("an unfrozen middleware list at bind time")
        chain.remove(middleware)
        chain.insert(0, middleware)
        LOG.warning("comfyui-headless: moved token authentication back to the front of the middleware chain")


class _FailureLog:
    """Rate-limited warning log of rejected requests, one line per address per interval."""

    def __init__(self, interval=30.0, limit=1024):
        self.interval = interval
        self.limit = limit
        self._seen = {}

    def note(self, remote, path, reason):
        now = time.monotonic()
        key = remote or "unknown"
        last, skipped = self._seen.get(key, (float("-inf"), 0))
        if now - last < self.interval:
            self._seen[key] = (last, skipped + 1)
            return
        extra = f"; {skipped} more since the last report" if skipped else ""
        LOG.warning("comfyui-headless: rejected %s from %s (%s)%s", path, key, reason, extra)
        if len(self._seen) >= self.limit:
            self._seen.clear()
        self._seen[key] = (now, 0)


def _store_error(message):
    """Log an unreadable token file."""
    LOG.error("comfyui-headless: %s. Every request is rejected until the token file is fixed.", message)


def presented_token(request, allow_query: bool) -> str | None:
    """Return the token from Authorization: Bearer, X-API-Key or ?token=, in that order."""
    header = request.headers.get("Authorization", "").strip()
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        return key
    if allow_query:
        value = request.query.get("token", "").strip()
        if value:
            return value
    return None


def _split_paths(paths):
    """Split block paths into exact paths and prefixes (entries ending in *)."""
    exact = frozenset(p for p in paths if not p.endswith("*"))
    prefixes = tuple(p[:-1] for p in paths if p.endswith("*"))
    return exact, prefixes


def build_middleware(runtime: dict):
    """Build the guard middleware.

    Args:
        runtime: Launcher settings with "auth", "headless" and "cors_origin".
    Returns:
        aiohttp middleware; its verifier attribute is the token Verifier, or None with auth off.
    """
    from aiohttp import web

    auth = runtime["auth"]
    headless = runtime["headless"]
    verifier = Verifier(auth["tokens_file"], on_error=_store_error) if auth["enabled"] else None
    public = frozenset(auth["public_paths"])
    allow_query = auth["allow_query_token"]
    exact, prefixes = _split_paths(headless["block_paths"])
    block_static = headless["block_static"]
    cors_origin = runtime.get("cors_origin")
    failures = _FailureLog()

    def mark(response):
        response.headers[MARKER_HEADER] = "1"
        if cors_origin:
            response.headers["Access-Control-Allow-Origin"] = cors_origin
            response.headers["Access-Control-Allow-Methods"] = _CORS_METHODS
            response.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    def is_static(request):
        resource = getattr(getattr(request.match_info, "route", None), "resource", None)
        return isinstance(resource, web.StaticResource)

    @web.middleware
    async def comfyui_headless_guard(request, handler):
        path = request.path
        if verifier is not None and request.rel_url.raw_path not in public:
            if (request.method == "OPTIONS" and cors_origin
                    and "Access-Control-Request-Method" in request.headers):
                return mark(web.Response(status=204))
            token = presented_token(request, allow_query)
            if verifier.verify(token) is None:
                if path != SELF_TEST_PATH:
                    failures.note(request.remote, path, "invalid token" if token else "no token")
                response = web.json_response(
                    {"error": "unauthorized", "message": "Send Authorization: Bearer <token>."}, status=401)
                response.headers["WWW-Authenticate"] = 'Bearer realm="comfyui-headless"'
                return mark(response)
        if path == HEALTH_PATH:
            return mark(web.json_response({"status": "ok"}))
        if path in exact or path.startswith(prefixes) or (block_static and is_static(request)):
            return mark(web.json_response({"error": "not found"}, status=404))
        return await handler(request)

    comfyui_headless_guard.verifier = verifier
    return comfyui_headless_guard
