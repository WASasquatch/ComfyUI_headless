import importlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest

from comfyui_headless import guard, tokens

HAVE_AIOHTTP = importlib.util.find_spec("aiohttp") is not None

if HAVE_AIOHTTP:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase
else:
    AioHTTPTestCase = unittest.TestCase


def runtime_for(folder, **auth):
    settings = {"enabled": True, "tokens_file": os.path.join(folder, "tokens.json"),
                "allow_query_token": False, "public_paths": [guard.HEALTH_PATH]}
    settings.update(auth)
    return {
        "comfyui_path": folder,
        "auth": settings,
        "headless": {"block_static": True,
                     "block_paths": ["/", "/extensions", "/api/extensions", "/templates/*", "/docs/*"]},
        "cors_origin": None,
    }


async def ok(request):
    return web.json_response({"path": request.path})


async def websocket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json({"type": "status"})
    await ws.close()
    return ws


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp not installed")
class MiddlewareTest(AioHTTPTestCase):
    query_tokens = False

    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = self.tmp.name
        self.token, _ = tokens.create(os.path.join(folder, "tokens.json"), name="test")
        static_dir = os.path.join(folder, "static")
        os.makedirs(static_dir)
        with open(os.path.join(static_dir, "app.js"), "w", encoding="utf-8") as handle:
            handle.write("frontend")
        self.runtime = runtime_for(folder, allow_query_token=self.query_tokens)
        app = web.Application()
        app.middlewares.insert(0, guard.build_middleware(self.runtime))
        app.router.add_get("/", ok)
        app.router.add_get("/queue", ok)
        app.router.add_get("/extensions", ok)
        app.router.add_get("/templates/{path:.*}", ok)
        app.router.add_post("/prompt", ok)
        app.router.add_route("*", "/anything", ok)
        app.router.add_get("/ws", websocket)
        internal = web.Application()
        internal.router.add_get("/logs", ok)
        app.add_subapp("/internal", internal)
        app.router.add_static("/", static_dir)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.tmp.cleanup()

    def bearer(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    async def test_rejects_without_token(self):
        response = await self.client.get("/queue")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers[guard.MARKER_HEADER], "1")
        self.assertIn("Bearer", response.headers["WWW-Authenticate"])

    async def test_rejects_bad_token(self):
        response = await self.client.get("/queue", headers=self.bearer(self.token[:-2] + "zz"))
        self.assertEqual(response.status, 401)

    async def test_accepts_bearer_and_api_key(self):
        response = await self.client.get("/queue", headers=self.bearer())
        self.assertEqual(response.status, 200)
        response = await self.client.get("/queue", headers={"X-API-Key": self.token})
        self.assertEqual(response.status, 200)
        response = await self.client.post("/prompt", headers={"authorization": f"bearer {self.token}"})
        self.assertEqual(response.status, 200)

    async def test_every_method_needs_a_token(self):
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            response = await self.client.request(method, "/anything")
            self.assertEqual(response.status, 401, method)

    async def test_subapp_needs_a_token(self):
        self.assertEqual((await self.client.get("/internal/logs")).status, 401)
        self.assertEqual((await self.client.get("/internal/logs", headers=self.bearer())).status, 200)

    async def test_query_token_off_by_default(self):
        response = await self.client.get(f"/queue?token={self.token}")
        self.assertEqual(response.status, 401)

    async def test_health_is_public(self):
        response = await self.client.get(guard.HEALTH_PATH)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "ok"})

    async def test_health_variants_are_not_public(self):
        for path in (guard.HEALTH_PATH + "/", "/headless/health/../queue", "/headless%2Fhealth", "/anything"):
            response = await self.client.get(path)
            self.assertNotEqual(response.status, 200, path)

    async def test_frontend_blocked(self):
        for path in ("/", "/app.js", "/extensions", "/templates/x.json"):
            response = await self.client.get(path, headers=self.bearer())
            self.assertEqual(response.status, 404, path)

    async def test_websocket_needs_a_token(self):
        response = await self.client.get("/ws", headers={"Connection": "Upgrade", "Upgrade": "websocket",
                                                         "Sec-WebSocket-Version": "13",
                                                         "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        self.assertEqual(response.status, 401)
        ws = await self.client.ws_connect("/ws", headers=self.bearer())
        message = await ws.receive_json()
        self.assertEqual(message["type"], "status")
        await ws.close()

    async def test_self_test_path_rejected_without_logging(self):
        with self.assertNoLogs("comfyui_headless", level="WARNING"):
            response = await self.client.get(guard.SELF_TEST_PATH)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers[guard.MARKER_HEADER], "1")

    async def test_revoked_token_stops_working(self):
        self.assertEqual((await self.client.get("/queue", headers=self.bearer())).status, 200)
        tokens.revoke(self.runtime["auth"]["tokens_file"], "test")
        middleware = self.app.middlewares[0]
        middleware.verifier._checked = float("-inf")
        self.assertEqual((await self.client.get("/queue", headers=self.bearer())).status, 401)


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp not installed")
class QueryTokenTest(MiddlewareTest):
    query_tokens = True

    async def test_query_token_off_by_default(self):
        response = await self.client.get(f"/queue?token={self.token}")
        self.assertEqual(response.status, 200)


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp not installed")
class CorsPreflightTest(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        runtime = runtime_for(self.tmp.name)
        runtime["cors_origin"] = "https://app.example"
        app = web.Application()
        app.middlewares.insert(0, guard.build_middleware(runtime))
        app.router.add_route("*", "/anything", ok)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.tmp.cleanup()

    async def test_preflight_answered_without_handler(self):
        response = await self.client.options("/anything", headers={"Origin": "https://app.example",
                                                                   "Access-Control-Request-Method": "POST"})
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://app.example")
        self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])
        response = await self.client.get("/anything")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://app.example")


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp not installed")
class PatchServerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server_py = os.path.join(self.tmp.name, "server.py")
        with open(self.server_py, "w", encoding="utf-8") as handle:
            handle.write("")
        guard._state["runtime"] = runtime_for(self.tmp.name)

    def tearDown(self):
        guard._state["runtime"] = None
        self.tmp.cleanup()

    def fake_module(self, with_server=True):
        module = types.ModuleType("server")
        module.__file__ = self.server_py

        class PromptServer:
            def __init__(self):
                self.app = web.Application(middlewares=[])
                self.bound = False

            async def start_multi_address(self, addresses):
                self.bound = True

        if with_server:
            module.PromptServer = PromptServer
        return module

    async def test_middleware_first_at_bind(self):
        module = self.fake_module()
        guard._patch_server(module)
        server = module.PromptServer()
        ours = server.app.middlewares[0]
        self.assertEqual(ours.__name__, "comfyui_headless_guard")

        @web.middleware
        async def other(request, handler):
            return await handler(request)

        server.app.middlewares.insert(0, other)
        await server.start_multi_address([])
        self.assertTrue(server.bound)
        self.assertIs(server.app.middlewares[0], ours)

    def test_template_lookups_quiet_when_package_missing(self):
        module = self.fake_module()

        class FrontendManager:
            @classmethod
            def legacy_templates_path(cls):
                raise AssertionError("not patched")

            @classmethod
            def embedded_docs_path(cls):
                return "docs"

        module.FrontendManager = FrontendManager
        original = guard.TEMPLATES_PACKAGE
        guard.TEMPLATES_PACKAGE = "chl_package_that_does_not_exist"
        try:
            guard._patch_server(module)
        finally:
            guard.TEMPLATES_PACKAGE = original
        self.assertIsNone(FrontendManager.legacy_templates_path())
        self.assertEqual(FrontendManager.embedded_docs_path(), "docs")

    def test_missing_prompt_server_is_incompatible(self):
        with self.assertRaises(SystemExit) as caught:
            guard._patch_server(self.fake_module(with_server=False))
        self.assertEqual(caught.exception.code, guard.EXIT_INCOMPATIBLE)

    def test_foreign_server_module_is_incompatible(self):
        module = self.fake_module()
        module.__file__ = os.path.join(self.tmp.name, "elsewhere.py")
        with self.assertRaises(SystemExit):
            guard._patch_server(module)

    def test_server_without_app_is_incompatible(self):
        module = types.ModuleType("server")
        module.__file__ = self.server_py

        class PromptServer:
            def __init__(self):
                self.routes = []

        module.PromptServer = PromptServer
        guard._patch_server(module)
        with self.assertRaises(SystemExit) as caught:
            PromptServer()
        self.assertEqual(caught.exception.code, guard.EXIT_INCOMPATIBLE)


class AfterImportTest(unittest.TestCase):
    def test_callback_runs_once_after_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            name = "chl_after_import_probe"
            with open(os.path.join(folder, name + ".py"), "w", encoding="utf-8") as handle:
                handle.write("VALUE = 42\n")
            seen = []
            sys.path.insert(0, folder)
            try:
                finder = guard.after_import(name, lambda module: seen.append(module.VALUE))
                importlib.import_module(name)
                self.assertEqual(seen, [42])
                self.assertNotIn(finder, sys.meta_path)
            finally:
                sys.path.remove(folder)
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
