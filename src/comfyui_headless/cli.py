"""Command-line interface: init, install, check, run and token management."""

import argparse
import os
import sys

from . import __version__, compat, config as config_mod, install as install_mod, launch, tokens


def _err(message):
    """Print an error line to stderr."""
    sys.stderr.write(f"comfyui-headless: error: {message}\n")


def _load(ns, project_root):
    """Return the Config named by --config, the environment or the default search."""
    path = config_mod.find_config(ns.config, project_root)
    return config_mod.load(path, root=project_root or os.getcwd())


def cmd_init(ns, extra, project_root):
    target = os.path.abspath(ns.config or os.path.join(os.getcwd(), config_mod.CONFIG_NAME))
    if os.path.exists(target) and not ns.force:
        _err(f"{target} exists. Pass --force to overwrite it.")
        return 1
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(config_mod.template())
    print(f"Wrote {target} (ComfyUI pin {compat.TESTED_PIN[:12]}).")
    return 0


def cmd_install(ns, extra, project_root):
    cfg = _load(ns, project_root)
    return install_mod.install(cfg, ref=ns.ref, force=ns.force, python_deps=not ns.no_python)


def cmd_check(ns, extra, project_root):
    cfg = _load(ns, project_root)
    report = compat.check(cfg.comfyui_path)
    tested = "tested" if report.tested else f"not tested (pin {compat.TESTED_PIN[:12]})"
    print(f"config      {cfg.describe()}")
    print(f"ComfyUI     {cfg.comfyui_path}")
    print(f"commit      {report.label()}, {tested}")
    for probe in compat.PROBES:
        state = "ok" if probe in report.passed else ("MISSING" if probe.required else "missing (optional)")
        print(f"  {probe.name:<24}{state}")
    python = cfg.python()
    print(f"python      {python or 'not found (run install)'}")
    if cfg.auth_enabled:
        try:
            records = tokens.load(cfg.tokens_file)
            active = sum(1 for r in records if not r.expired())
            print(f"tokens      {active} active in {cfg.tokens_file}")
        except tokens.TokenStoreError as exc:
            print(f"tokens      ERROR: {exc}")
    else:
        print("tokens      authentication off")
    print(f"listen      {cfg.listen}:{cfg.port}")
    if not report.compatible:
        print()
        print(compat.incompatible_message(report))
        return compat.EXIT_INCOMPATIBLE
    print("result      compatible")
    return 0


def cmd_run(ns, extra, project_root):
    cfg = _load(ns, project_root)
    return launch.run(cfg, extra)


def cmd_token_create(ns, extra, project_root):
    cfg = _load(ns, project_root)
    try:
        lifetime = tokens.parse_duration(ns.expires)
    except ValueError as exc:
        _err(str(exc))
        return 2
    token, record = tokens.create(cfg.tokens_file, name=ns.name, expires=lifetime)
    if ns.raw:
        print(token)
        return 0
    host = launch.probe_host(cfg.listen)
    shown = f"[{host}]" if ":" in host else host
    expiry = f"expires {record.expires}" if record.expires else "no expiry"
    print(f'Created token "{record.name}" (id {record.id}, {expiry}).')
    print("It is shown once and cannot be recovered:")
    print()
    print(f"  {token}")
    print()
    print("Send it on every request:")
    print(f"  Authorization: Bearer {token}")
    print()
    print("Example:")
    print(f'  curl -H "Authorization: Bearer {token}" http://{shown}:{cfg.port}/system_stats')
    if not cfg.auth_enabled:
        print()
        print(f"Note: [auth] enabled = false in {cfg.describe()}; the server does not check tokens.")
    return 0


def cmd_token_list(ns, extra, project_root):
    cfg = _load(ns, project_root)
    records = tokens.load(cfg.tokens_file)
    if not records:
        print(f"No tokens in {cfg.tokens_file}.")
        return 0
    print(f"{'ID':<10}{'NAME':<26}{'CREATED':<22}{'EXPIRES':<22}STATUS")
    for r in records:
        status = "expired" if r.expired() else "active"
        print(f"{r.id:<10}{r.name:<26}{r.created:<22}{(r.expires or 'never'):<22}{status}")
    return 0


def cmd_token_revoke(ns, extra, project_root):
    cfg = _load(ns, project_root)
    record = tokens.revoke(cfg.tokens_file, ns.key)
    print(f'Revoked token "{record.name}" (id {record.id}). A running server stops accepting it within a second.')
    return 0


def cmd_version(ns, extra, project_root):
    print(f"comfyui-headless {__version__}, ComfyUI pin {compat.TESTED_PIN}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="comfyui-headless",
        description="Install and run ComfyUI as an API-only server with token authentication.",
        epilog="ComfyUI arguments for run go after --, e.g. run -- --lowvram")
    parser.add_argument("--config", metavar="PATH",
                        help=f"config file (default: ${config_mod.ENV_CONFIG} or ./{config_mod.CONFIG_NAME})")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help="config file")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("init", help=f"write a default {config_mod.CONFIG_NAME}", parents=[common])
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("install", help="clone ComfyUI at the pinned ref and install its Python packages", parents=[common])
    p.add_argument("--ref", help="commit, tag or branch; overrides [comfyui] ref")
    p.add_argument("--force", action="store_true", help="check out over local changes")
    p.add_argument("--no-python", action="store_true", help="skip torch and requirements")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("check", help="report ComfyUI compatibility, Python and tokens", parents=[common])
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="start the headless server", parents=[common])
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("token", help="create, list or revoke tokens", parents=[common])
    token_sub = p.add_subparsers(dest="token_command", metavar="ACTION")
    q = token_sub.add_parser("create", help="create a token and print it once", parents=[common])
    q.add_argument("--name", help="unique label (default: token-<id>)")
    q.add_argument("--expires", default="never", help="lifetime such as 90m, 12h, 30d, 8w, or never")
    q.add_argument("--raw", action="store_true", help="print only the token")
    q.set_defaults(func=cmd_token_create)
    q = token_sub.add_parser("list", help="list tokens", parents=[common])
    q.set_defaults(func=cmd_token_list)
    q = token_sub.add_parser("revoke", help="revoke a token by id or name", parents=[common])
    q.add_argument("key", metavar="ID_OR_NAME")
    q.set_defaults(func=cmd_token_revoke)
    p.set_defaults(func=None, token_parser=p)

    p = sub.add_parser("version", help="print the harness version and ComfyUI pin", parents=[common])
    p.set_defaults(func=cmd_version)
    return parser


def main(argv=None, project_root=None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments; defaults to sys.argv[1:].
        project_root: Checkout folder when started through headless.py.
    Returns:
        Exit status.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1:]
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    if ns.func is None:
        ns.token_parser.print_help()
        return 2
    if extra and ns.command != "run":
        _err("arguments after -- are only accepted by run.")
        return 2
    try:
        return ns.func(ns, extra, project_root)
    except (config_mod.ConfigError, tokens.TokenStoreError, install_mod.InstallError, launch.LaunchError) as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
