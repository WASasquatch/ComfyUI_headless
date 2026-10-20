# ComfyUI_headless

ComfyUI as an API-only server: a pinned install, no frontend, and bearer-token authentication on every route.

ComfyUI's files are never modified. The launcher runs ComfyUI's own `main.py` and attaches an aiohttp
middleware to its server as it starts. A ComfyUI version without the parts the middleware attaches to stops
with **"The version of ComfyUI is incompatible"** and never starts unauthenticated.

## Requirements

- Python 3.11 or newer for the harness. ComfyUI's own environment needs 3.10 or newer.
- git, for `install`.
- No other packages. The harness is standard library only; inside ComfyUI it uses ComfyUI's aiohttp.

## Quick start

Linux:

```sh
git clone https://github.com/WASasquatch/ComfyUI_headless
cd ComfyUI_headless
./headless.sh install
./headless.sh token create --name my-client
./headless.sh run
```

Windows, in CMD (in PowerShell, type `.\headless` for `headless`):

```bat
git clone https://github.com/WASasquatch/ComfyUI_headless
cd ComfyUI_headless
headless install
headless token create --name my-client
headless run
```

`install` clones ComfyUI at the tested commit into `ComfyUI/`, creates `.venv/`, installs torch from the CUDA 13.0
index and installs ComfyUI's requirements without the frontend packages. `run` serves the API on
`127.0.0.1:8188` and prints `API ready at http://127.0.0.1:8188` once its startup self-test passes.

`pip install .` installs the same commands as `comfyui-headless`. `HEADLESS_PYTHON` selects the interpreter the
launchers use.

## Commands

| Command | Does |
| --- | --- |
| `init [--force]` | Writes `headless.toml` with every setting and the tested pin. |
| `install [--ref REF] [--force] [--no-python]` | Clones or updates ComfyUI at the ref, checks compatibility, installs torch and requirements. |
| `check` | Prints the compatibility report, the interpreter and the token count. Exits 78 when incompatible. |
| `run [-- COMFYUI_ARGS...]` | Starts the server. Arguments after `--` go to ComfyUI, e.g. `run -- --lowvram`. |
| `token create [--name N] [--expires 30d] [--raw]` | Creates a token and prints it once. `--raw` prints the token alone. |
| `token list` | Lists ids, names, creation and expiry times. |
| `token revoke ID_OR_NAME` | Removes a token. A running server rejects it within a second. |
| `version` | Prints the harness version and the ComfyUI pin. |

`--config PATH` works before or after the command.

## Configuration

`init` writes `headless.toml`. Without a file the built-in defaults apply. The file is found in this order:
`--config`, `$COMFYUI_HEADLESS_CONFIG`, `./headless.toml`, then `headless.toml` in the checkout. Relative paths
resolve from the file's folder.

| Key | Default | Meaning |
| --- | --- | --- |
| `[comfyui] repo` | `https://github.com/Comfy-Org/ComfyUI.git` | Clone source. |
| `[comfyui] ref` | the tested pin | Commit, tag or branch `install` checks out. |
| `[comfyui] path` | `ComfyUI` | ComfyUI folder. |
| `[comfyui] manage` | `true` | `false` runs the ComfyUI at `path` as it is; `install` never clones or checks out. |
| `[python] executable` | `""` | Interpreter for ComfyUI. Empty uses `venv`. |
| `[python] venv` | `.venv` | Virtual environment `install` creates. |
| `[python] flags` | `["-s"]` | Interpreter flags for ComfyUI and pip. |
| `[python] install_torch` | `true` | Install `torch_packages` before the requirements. |
| `[python] torch_index_url` | `https://download.pytorch.org/whl/cu130` | Torch index: `cu130`, `cu128`, `cpu`, `rocm7.2`, `xpu`. Empty uses PyPI. |
| `[python] torch_packages` | `["torch", "torchvision", "torchaudio"]` | Torch packages. |
| `[python] skip_packages` | the three frontend packages | Requirements left out of the install. |
| `[server] listen` | `127.0.0.1` | Bind address; a comma-separated list binds several. |
| `[server] port` | `8188` | Port. |
| `[server] args` | `[]` | Extra ComfyUI arguments. `--listen`, `--port` and the `--front-end-*` flags are refused. |
| `[auth] enabled` | `true` | Token authentication. |
| `[auth] tokens_file` | `tokens.json` | Token store. |
| `[auth] allow_query_token` | `false` | Accept `?token=` on any URL. |
| `[auth] public_paths` | `["/headless/health"]` | Exact paths served without a token. |
| `[headless] block_static` | `true` | 404 for every static file route, custom node web folders included. |
| `[headless] block_paths` | `["/", "/extensions", "/api/extensions", "/templates/*", "/docs/*"]` | 404 paths; a trailing `*` matches a prefix. |
| `[state] dir` | `.headless` | Runtime files: the empty web root, `runtime.json`, `state.json`, filtered requirements. |

## Tokens

A token reads `chl_<id>_<secret>`: an 8-character hex id and a 43-character random secret (256 bits). The store
holds the id, name, times and the SHA-256 of the token, never the token. `tokens.json` is written atomically and
is owner-only (`0600`) on Linux and macOS.

Requests carry the token in one of:

```text
Authorization: Bearer chl_...
X-API-Key: chl_...
?token=chl_...            only with [auth] allow_query_token = true
```

Every route on ComfyUI's server requires a token: HTTP, `/ws`, `/view`, `/upload/*`, `/internal/*`, the `/api/*`
aliases and routes added by custom nodes. A rejected request gets `401` with `WWW-Authenticate: Bearer`.
`/headless/health` is public by default and returns `{"status": "ok"}`. With `--enable-cors-header`, CORS
preflight requests are answered without a token and nothing else is.

## Calling the API

`/prompt` takes the API format. In ComfyUI, **Workflow > Export (API)** saves a workflow in that format; the
canvas format that **Save** writes is not accepted.

```sh
TOKEN=chl_...
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"prompt": '"$(cat workflow_api.json)"'}' http://127.0.0.1:8188/prompt
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8188/history/<prompt_id>
curl -H "Authorization: Bearer $TOKEN" -o out.png \
     "http://127.0.0.1:8188/view?filename=ComfyUI_00001_.png&type=output"
```

Progress over the websocket, with `pip install websocket-client`:

```python
import json, websocket

ws = websocket.create_connection("ws://127.0.0.1:8188/ws?clientId=my-client",
                                 header=["Authorization: Bearer chl_..."])
while True:
    message = ws.recv()
    if isinstance(message, str):
        print(json.loads(message)["type"])
```

Send the same `clientId` in the `/prompt` body (`"client_id": "my-client"`) to receive that prompt's
`progress`, `executing` and `executed` events.

## Pinning and compatibility

| Tested ComfyUI commit | Version | Date |
| --- | --- | --- |
| `8f04fd6ecf4a548937971e840af2a985529fab76` | 0.42.0 | 2026-10-19 |

`[comfyui] ref` selects what `install` checks out, detached. `install --ref master` overrides it for one
install. `run` warns when the checkout is not a tested commit or differs from `ref`, and starts anyway after the
checks below pass.

Startup is checked at three points:

1. Before launch, `check` and `run` read ComfyUI's source for the entry point, the `PromptServer` aiohttp
   application and the `--listen` and `--port` flags. A miss stops with the incompatibility message and exit
   code 78.
2. Inside ComfyUI, the guard confirms that `server` is the ComfyUI folder's own `server.py`, that
   `PromptServer.app` is an unfrozen aiohttp application, and, just before binding, that the authentication
   middleware is first in the chain. A miss exits with code 78 before the port opens.
3. Once the port opens, the launcher sends an unauthenticated request. Anything but `401` from the guard stops
   ComfyUI with exit code 70.

Optional parts fall back. Without `--front-end-root` in ComfyUI, `install` keeps `comfyui-frontend-package` and
the middleware answers its files with 404. Without `--disable-auto-launch`, ComfyUI's own launch setting applies.

An existing install, such as the Windows portable build, runs with `manage = false` and no `install` step:

```toml
[comfyui]
path = 'C:\ComfyUI_windows_portable\ComfyUI'
manage = false

[python]
executable = 'C:\ComfyUI_windows_portable\python_embeded\python.exe'
```

## What is removed

- The frontend: `/` and every static route return 404, `--front-end-root` points at an empty folder, and the
  `comfyui-frontend-package`, `comfyui-workflow-templates` and `comfyui-embedded-docs` packages are not installed.
- `/extensions`, `/templates/*` and `/docs/*`.
- The browser launch: `--disable-auto-launch` is passed whenever ComfyUI has the flag.

## Security notes

- The default bind address is `127.0.0.1`. `listen = "0.0.0.0"` exposes the API to the network; serve it over
  TLS with `args = ["--tls-keyfile", "key.pem", "--tls-certfile", "cert.pem"]` or a reverse proxy.
- Every token has full API access: queueing, uploads, outputs, user data, interrupt and memory release.
- The guard covers ComfyUI's server. A custom node that opens its own server on another port is outside it.
- `?token=` values appear in access logs, proxies and browser history.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success, or ComfyUI stopped normally. |
| 1 | Configuration, install, token or launch error; the message names the fix. |
| 2 | Usage error. |
| 70 | The startup self-test failed and ComfyUI was stopped. |
| 78 | The version of ComfyUI is incompatible. |
| 130 | Interrupted. |

## Development

```sh
python -m pip install aiohttp
python -m unittest discover -s tests -t .
python scripts/smoke_test.py --config headless.toml -- --cpu
```

`scripts/smoke_test.py` starts the server from the config, runs the authentication, frontend, prompt,
websocket and revocation checks, and stops it. CI runs it on every push against the pin, and weekly against
ComfyUI `master`.

## License

MIT. ComfyUI is GPL-3.0 and is downloaded from its own repository; none of its code is in this one.
