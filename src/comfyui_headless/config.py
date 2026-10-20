"""headless.toml loading, defaults and validation."""

import os
import shutil
import sys
import tomllib

from .compat import TESTED_PIN

CONFIG_NAME = "headless.toml"
ENV_CONFIG = "COMFYUI_HEADLESS_CONFIG"

_STR = "string"
_BOOL = "boolean"
_INT = "integer"
_STRS = "list of strings"

SCHEMA = {
    "comfyui": {
        "repo": (_STR, "https://github.com/Comfy-Org/ComfyUI.git"),
        "ref": (_STR, TESTED_PIN),
        "path": (_STR, "ComfyUI"),
        "manage": (_BOOL, True),
    },
    "python": {
        "executable": (_STR, ""),
        "venv": (_STR, ".venv"),
        "flags": (_STRS, ["-s"]),
        "install_torch": (_BOOL, True),
        "torch_index_url": (_STR, "https://download.pytorch.org/whl/cu130"),
        "torch_packages": (_STRS, ["torch", "torchvision", "torchaudio"]),
        "skip_packages": (_STRS, ["comfyui-frontend-package", "comfyui-workflow-templates", "comfyui-embedded-docs"]),
    },
    "server": {
        "listen": (_STR, "127.0.0.1"),
        "port": (_INT, 8188),
        "args": (_STRS, []),
    },
    "auth": {
        "enabled": (_BOOL, True),
        "tokens_file": (_STR, "tokens.json"),
        "allow_query_token": (_BOOL, False),
        "public_paths": (_STRS, ["/headless/health"]),
    },
    "headless": {
        "block_static": (_BOOL, True),
        "block_paths": (_STRS, ["/", "/extensions", "/api/extensions", "/templates/*", "/docs/*"]),
    },
    "state": {
        "dir": (_STR, ".headless"),
    },
}

TEMPLATE = """\
# comfyui-headless configuration. Relative paths resolve from this file's folder.

[comfyui]
repo = "https://github.com/Comfy-Org/ComfyUI.git"
# Commit, tag or branch to install. The default is the newest tested commit.
ref = "@PIN@"
path = "ComfyUI"
# false: use the ComfyUI already at path; install never clones or checks out.
manage = true

[python]
# Interpreter that runs ComfyUI. Empty: the venv below, created by install.
executable = ""
venv = ".venv"
flags = ["-s"]
install_torch = true
# cu130, cu128, cpu, rocm7.2, xpu: https://download.pytorch.org/whl/<name>
torch_index_url = "https://download.pytorch.org/whl/cu130"
torch_packages = ["torch", "torchvision", "torchaudio"]
skip_packages = ["comfyui-frontend-package", "comfyui-workflow-templates", "comfyui-embedded-docs"]

[server]
listen = "127.0.0.1"
port = 8188
# Extra ComfyUI arguments, e.g. ["--lowvram", "--preview-method", "none"].
args = []

[auth]
enabled = true
tokens_file = "tokens.json"
# Accept ?token= on any URL, for clients that cannot send headers.
allow_query_token = false
public_paths = ["/headless/health"]

[headless]
block_static = true
block_paths = ["/", "/extensions", "/api/extensions", "/templates/*", "/docs/*"]

[state]
dir = ".headless"
"""


class ConfigError(Exception):
    """Raised when headless.toml is missing, unreadable or invalid."""


def template() -> str:
    """Return the default headless.toml text with the tested pin filled in."""
    return TEMPLATE.replace("@PIN@", TESTED_PIN)


def venv_python(venv: str) -> str:
    """Return the interpreter path inside a virtual environment folder."""
    if os.name == "nt":
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def _matches(kind, value):
    """Return True when value has the schema type kind."""
    if kind == _STR:
        return isinstance(value, str)
    if kind == _BOOL:
        return isinstance(value, bool)
    if kind == _INT:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _warn(message):
    """Print a configuration warning to stderr."""
    sys.stderr.write(f"comfyui-headless: warning: {message}\n")


class Config:
    """Validated settings with absolute paths.

    Attributes:
        path: Config file, or None for built-in defaults.
        base: Folder that relative paths resolve from.
        data: Section tables with defaults applied.
    """

    def __init__(self, path, base, data):
        self.path = os.path.abspath(path) if path else None
        self.base = base
        self.data = data
        comfyui, python, server, auth, headless = (
            data[k] for k in ("comfyui", "python", "server", "auth", "headless"))
        self.repo = comfyui["repo"]
        self.ref = comfyui["ref"].strip()
        self.manage = comfyui["manage"]
        self.comfyui_path = self.resolve(comfyui["path"])
        self.python_executable = python["executable"].strip()
        self.venv_path = self.resolve(python["venv"])
        self.python_flags = list(python["flags"])
        self.install_torch = python["install_torch"]
        self.torch_index_url = python["torch_index_url"].strip()
        self.torch_packages = list(python["torch_packages"])
        self.skip_packages = list(python["skip_packages"])
        self.listen = server["listen"].strip()
        self.port = server["port"]
        self.server_args = list(server["args"])
        self.auth_enabled = auth["enabled"]
        self.tokens_file = self.resolve(auth["tokens_file"])
        self.allow_query_token = auth["allow_query_token"]
        self.public_paths = list(auth["public_paths"])
        self.block_static = headless["block_static"]
        self.block_paths = list(headless["block_paths"])
        self.state_dir = self.resolve(data["state"]["dir"])

    def resolve(self, value: str) -> str:
        """Return value as an absolute path, relative to base."""
        return os.path.normpath(os.path.join(self.base, os.path.expanduser(value)))

    def describe(self) -> str:
        """Return the config file path, or a label for built-in defaults."""
        return self.path or f"the built-in defaults (no {CONFIG_NAME} in {self.base})"

    def python(self) -> str | None:
        """Return the interpreter that runs ComfyUI, or None when it does not exist."""
        exe = self.python_executable
        if exe:
            if os.sep in exe or "/" in exe:
                path = self.resolve(exe)
                return path if os.path.isfile(path) else None
            return shutil.which(exe)
        path = venv_python(self.venv_path)
        return path if os.path.isfile(path) else None

    def validate(self):
        """Check value ranges.

        Raises:
            ConfigError: on the first invalid value.
        """
        where = self.describe()
        if not self.ref:
            raise ConfigError(f"{where}: [comfyui] ref is empty.")
        if not self.listen:
            raise ConfigError(f"{where}: [server] listen is empty.")
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"{where}: [server] port must be between 1 and 65535.")
        for value in self.public_paths:
            if not value.startswith("/") or "*" in value:
                raise ConfigError(f"{where}: [auth] public_paths entries are exact paths starting with /; got {value!r}.")
        for value in self.block_paths:
            if not value.startswith("/") or "*" in value[:-1]:
                raise ConfigError(f"{where}: [headless] block_paths entries start with / and may end in *; got {value!r}.")


def find_config(explicit: str | None = None, project_root: str | None = None) -> str | None:
    """Locate headless.toml.

    Args:
        explicit: Path from --config.
        project_root: Checkout folder when run through headless.py.
    Returns:
        Path, or None when no file exists and none was named.
    Raises:
        ConfigError: when a named file does not exist.
    """
    named = explicit or os.environ.get(ENV_CONFIG)
    if named:
        if not os.path.isfile(named):
            raise ConfigError(f"Config file {named} does not exist.")
        return os.path.abspath(named)
    for base in (os.getcwd(), project_root):
        if base:
            candidate = os.path.join(base, CONFIG_NAME)
            if os.path.isfile(candidate):
                return candidate
    return None


def load(path: str | None = None, root: str | None = None) -> Config:
    """Load and validate settings.

    Args:
        path: headless.toml path, or None for built-in defaults.
        root: Base folder for relative paths when path is None.
    Returns:
        Config.
    Raises:
        ConfigError: on unreadable TOML or invalid values.
    """
    raw = {}
    if path:
        try:
            with open(path, "rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError:
            raise ConfigError(f"Config file {path} does not exist.") from None
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"{path}: {exc}") from None
        base = os.path.dirname(os.path.abspath(path))
    else:
        base = os.path.abspath(root or os.getcwd())
    label = path or "defaults"
    data = {}
    for section, keys in SCHEMA.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            raise ConfigError(f"{label}: [{section}] must be a table.")
        values = {}
        for key, (kind, default) in keys.items():
            if key in given:
                value = given[key]
                if not _matches(kind, value):
                    raise ConfigError(f"{label}: [{section}] {key} must be a {kind}.")
            else:
                value = list(default) if isinstance(default, list) else default
            values[key] = value
        for key in given:
            if key not in keys:
                _warn(f"{label}: unknown key [{section}] {key} is ignored.")
        data[section] = values
    for section in raw:
        if section not in SCHEMA:
            _warn(f"{label}: unknown table [{section}] is ignored.")
    config = Config(path, base, data)
    config.validate()
    return config
