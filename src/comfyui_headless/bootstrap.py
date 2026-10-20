"""Child-process entry: installs the guard, then runs ComfyUI's main.py as __main__."""

import json
import os
import runpy
import sys


def import_path(path: str) -> str:
    """Return path in a form the import system accepts; Windows UNC paths use forward slashes."""
    if os.name == "nt" and path.startswith("\\\\"):
        return path.replace("\\", "/")
    return path


def main(argv=None):
    """Run ComfyUI under the guard.

    Args:
        argv: [runtime_json, *comfyui_args]; defaults to sys.argv[1:].
    Raises:
        SystemExit: with ComfyUI's exit status.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: bootstrap RUNTIME_JSON [COMFYUI_ARGS...]\n")
        raise SystemExit(2)
    with open(argv[0], "r", encoding="utf-8") as handle:
        runtime = json.load(handle)
    comfy_dir = os.path.abspath(runtime["comfyui_path"])
    main_py = os.path.join(comfy_dir, "main.py")

    from comfyui_headless import guard

    sys.path[:] = [entry for entry in sys.path if entry not in ("", ".")]
    sys.path.insert(0, import_path(comfy_dir))
    os.chdir(comfy_dir)
    guard.install(runtime)
    sys.argv = [main_py] + argv[1:]
    runpy.run_path(main_py, run_name="__main__")


def command(package_dir: str) -> str:
    """Return `python -c` code that loads this package from package_dir and calls main().

    Args:
        package_dir: Folder holding comfyui_headless/__init__.py.
    Returns:
        Python source for -c; adds nothing to sys.path.
    """
    package_dir = import_path(package_dir)
    init = import_path(os.path.join(package_dir, "__init__.py"))
    return "; ".join((
        "import importlib.util, sys",
        f"spec = importlib.util.spec_from_file_location('comfyui_headless', {init!r}, "
        f"submodule_search_locations=[{package_dir!r}])",
        "module = importlib.util.module_from_spec(spec)",
        "sys.modules['comfyui_headless'] = module",
        "spec.loader.exec_module(module)",
        "from comfyui_headless.bootstrap import main",
        "main()",
    ))
