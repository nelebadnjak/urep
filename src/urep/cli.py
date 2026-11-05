from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, NoReturn

import typer

from . import __version__
from .config import Config, ConfigError, make_node_id
from .daemon import Daemon
from .gitrepo import GitError, GitRepo, git_version
from .hlc import Clock
from .paths import PathError, mirror_rel
from .state import EventLog, RuntimeState
from .transport import TransportError, ping_peer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="uRep — micro replication for configuration files.",
)


def _fail(message: object) -> NoReturn:
    typer.secho(f"error: {message}", fg="red", err=True)
    raise typer.Exit(2)


def _load(require_token: bool) -> Config:
    try:
        return Config.load(require_token=require_token)
    except ConfigError as exc:
        _fail(exc)


def _cli_values(
    token: str | None,
    token_file: Path | None,
    prefix: str | None,
    tags: list[str] | None,
    paths: list[str] | None,
    roots: list[str] | None,
    slave: bool,
    listen: str | None,
    pre_hooks: list[str] | None,
    post_hooks: list[str] | None,
    log_level: str | None,
) -> dict[str, Any]:
    return {
        "token": token,
        "token_file": token_file,
        "prefix": prefix,
        "tags": tags,
        "paths": paths,
        "roots": roots,
        "slave": slave,
        "listen": listen,
        "pre_hooks": pre_hooks,
        "post_hooks": post_hooks,
        "log_level": log_level,
    }


def _run_daemon(values: dict[str, Any] | None = None) -> None:
    try:
        config = Config.load(cli=values, require_token=True)
    except ConfigError as exc:
        _fail(exc)
    try:
        Daemon(config).run()
    except GitError as exc:
        _fail(exc)
    except KeyboardInterrupt:
        pass


def _rel(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return mirror_rel(Path(path))
    except PathError as exc:
        _fail(exc)


def _read_peers(config: Config) -> list[dict[str, Any]]:
    try:
        data = json.loads((config.state_dir / "peers.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    peers = data.get("peers") if isinstance(data, dict) else None
    return peers if isinstance(peers, list) else []


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    token: str | None = typer.Option(None, "--token", help="Cluster token."),
    token_file: Path | None = typer.Option(None, "--token-file", help="Read token from file."),
    prefix: str | None = typer.Option(None, "--prefix", help="Readable node-id prefix."),
    tags: list[str] | None = typer.Option(None, "--tag", help="Tag to listen/send (repeatable)."),
    paths: list[str] | None = typer.Option(None, "--path", help="File to watch (repeatable)."),
    roots: list[str] | None = typer.Option(
        None, "--root", help="Accepted write root (repeatable)."
    ),
    slave: bool = typer.Option(False, "--slave", help="Receive-only mode."),
    listen: str | None = typer.Option(None, "--listen", help="TCP listen address."),
    pre_hooks: list[str] | None = typer.Option(
        None, "--pre-hook", help="Pre hook (repeatable)."
    ),
    post_hooks: list[str] | None = typer.Option(
        None, "--post-hook", help="Post hook (repeatable)."
    ),
    log_level: str | None = typer.Option(None, "--log-level", help="Log level."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _run_daemon(
        _cli_values(
            token, token_file, prefix, tags, paths, roots, slave, listen,
            pre_hooks, post_hooks, log_level,
        )
    )


@app.command()
def daemon(
    foreground: bool = typer.Option(False, "-f", "--foreground", help="Run in foreground."),
    slave: bool = typer.Option(False, "--slave", help="Receive-only mode."),
) -> None:
    _ = foreground
    _run_daemon({"slave": slave} if slave else None)


@app.command()
def init(
    token_file: Path = typer.Option(Path("/etc/urep/token"), "--token-file"),
    print_token: bool = typer.Option(False, "--print-token"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    if token_file.exists() and not force:
        _fail(f"token file already exists: {token_file} (use --force)")
    token = secrets.token_urlsafe(32)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    typer.echo(f"wrote {token_file}")
    if print_token:
        typer.echo(token)


@app.command("id")
def id_command(prefix: str | None = typer.Option(None, "--prefix")) -> None:
    try:
        typer.echo(make_node_id(prefix))
    except ConfigError as exc:
        _fail(exc)


@app.command()
def version() -> None:
    typer.echo(f"urep {__version__}")


@app.command()
def ping(address: str) -> None:
    config = _load(True)
    node = make_node_id(config.prefix)
    try:
        result = ping_peer(
            address,
            token=config.token.get_secret_value(),
            node_id=node,
            tags=config.tags,
            role=config.role,
        )
    except TransportError as exc:
        _fail(exc)
    tags = ",".join(result["tags"]) or "-"
    typer.echo(
        f"pong from {result['node_id']} role={result['role']} "
        f"tags={tags} rtt={result['rtt_ms']:.1f}ms"
    )


@app.command("config")
def config_command(json_output: bool = typer.Option(False, "--json")) -> None:
    config = _load(False)
    data = config.redacted()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return
    width = max(len(key) for key in data)
    for key, value in data.items():
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value) or "-"
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        else:
            rendered = str(value)
        typer.echo(f"{key:<{width}}  {rendered}")


@app.command()
def status(json_output: bool = typer.Option(False, "--json")) -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
    except GitError as exc:
        _fail(exc)
    state = repo.derive_state()
    runtime = RuntimeState(config.state_dir / "state.json")
    paused = runtime.paused()
    files = [item for item in state.values() if not item.deleted]
    tombstones = [item for item in state.values() if item.deleted]
    events = EventLog(config.state_dir / "events.jsonl").read()
    last = events[-1] if events else None
    color = "PAUSED" if paused else "GREEN"
    if not paused and last:
        result = last.get("result")
        level = last.get("level")
        if level == "error" or result in {
            "pre_hook_failed",
            "error",
            "path_not_allowed",
            "path_forbidden",
            "bad_hash",
        }:
            color = "RED"
        elif level == "warning" or result in {
            "hook_failed",
            "non_atomic_write",
            "too_large",
        }:
            color = "YELLOW"
    head = repo.head()
    peer_list = _read_peers(config)
    path_list = [str(path) for path in config.paths]
    root_list = [str(root) for root in config.accept_roots]
    data = {
        "node": None,
        "role": config.role,
        "color": color,
        "paused": paused,
        "tags": config.tags,
        "paths": path_list,
        "accept_roots": root_list,
        "repo": str(config.repo_dir),
        "head": head[:8] if head else None,
        "commits": repo.commit_count(),
        "files": len(files),
        "tombstones": len(tombstones),
        "peers": len(peer_list),
        "last_event": last,
    }
    if json_output:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    typer.echo(f"node        {data['node'] or '(generated at startup)'}")
    typer.echo(f"role        {data['role']}")
    typer.echo(f"color       {data['color']}")
    typer.echo(f"paused      {'yes' if paused else 'no'}")
    typer.echo(f"tags        {', '.join(config.tags) or '-'}")
    typer.echo(f"paths       {', '.join(path_list) or '-'}")
    typer.echo(f"roots       {', '.join(root_list) or '-'}")
    typer.echo(f"repo        {data['repo']}")
    typer.echo(f"commits     {data['commits']}")
    typer.echo(f"files       {data['files']}")
    typer.echo(f"tombstones  {data['tombstones']}")
    typer.echo(f"peers       {data['peers']}")
    typer.echo(f"head        {data['head'] or '-'}")
    if last:
        typer.echo(
            "last        "
            f"{last.get('action', '?')} {last.get('path', '?')} "
            f"result={last.get('result', '?')}"
        )


@app.command()
def doctor() -> None:
    config = _load(False)
    checks: list[tuple[str, str, str]] = []

    try:
        found = git_version()
        status_name = "ok" if found >= (2, 29) else "fail"
        checks.append((status_name, "git", f"{found[0]}.{found[1]}"))
    except GitError as exc:
        checks.append(("fail", "git", str(exc)))

    if (config.repo_dir / ".git").exists():
        try:
            repo = GitRepo(config.repo_dir)
            repo.ensure()
            fmt = repo.object_format()
            checks.append(
                ("ok" if fmt == "sha256" else "fail", "repo", f"{fmt} @ {config.repo_dir}")
            )
        except GitError as exc:
            checks.append(("fail", "repo", str(exc)))
    else:
        checks.append(
            ("info", "repo", f"not initialized at {config.repo_dir} (created on first use)")
        )

    try:
        watches = int(Path("/proc/sys/fs/inotify/max_user_watches").read_text().strip())
        checks.append(
            ("ok" if watches >= 8192 else "warn", "inotify", f"max_user_watches={watches}")
        )
    except OSError as exc:
        checks.append(("warn", "inotify", str(exc)))

    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=config.state_dir):
            pass
        checks.append(("ok", "state_dir", str(config.state_dir)))
    except OSError as exc:
        checks.append(("fail", "state_dir", str(exc)))

    for path in config.paths:
        if path.is_symlink():
            checks.append(("warn", "path", f"{path} is a symlink (skipped)"))
        elif path.exists() and path.is_dir():
            checks.append(("fail", "path", f"{path} is a directory (only files are supported)"))
        elif path.exists():
            checks.append(("ok", "path", str(path)))
        else:
            checks.append(("info", "path", f"{path} does not exist yet"))

    for root in config.accept_roots:
        checks.append(("ok" if root.exists() else "info", "root", str(root)))

    colors = {"ok": "green", "warn": "yellow", "fail": "red", "info": "blue"}
    for status_name, name, detail in checks:
        typer.secho(f"[{status_name}] {name}: {detail}", fg=colors[status_name])

    if any(status_name == "fail" for status_name, _, _ in checks):
        raise typer.Exit(1)


@app.command()
def log(
    path: str | None = typer.Argument(None),
    lines: int = typer.Option(20, "-n", "--lines"),
) -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
    except GitError as exc:
        _fail(exc)
    typer.echo(repo.log(_rel(path), lines), nl=False)


@app.command()
def show(rev: str) -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
        typer.echo(repo.show(rev), nl=False)
    except GitError as exc:
        _fail(exc)


@app.command("diff")
def diff_command(rev: str, path: str | None = typer.Argument(None)) -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
        typer.echo(repo.diff(rev, _rel(path)), nl=False)
    except GitError as exc:
        _fail(exc)


@app.command()
def revert(rev: str) -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
        node = make_node_id(config.prefix)
        clock = Clock(node)
        for item in repo.derive_state().values():
            clock.observe(item.version)
        sha = repo.revert(rev, node=node, clock=clock)
    except GitError as exc:
        _fail(exc)
    if sha:
        typer.echo(sha)
    else:
        typer.echo("nothing to commit")


@app.command()
def conflicts() -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
        items = repo.conflicts()
    except GitError as exc:
        _fail(exc)
    if not items:
        typer.echo("no conflicts")
        return
    for item in items:
        typer.echo(item)


@app.command()
def restore(ref: str) -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
        node = make_node_id(config.prefix)
        clock = Clock(node)
        for item in repo.derive_state().values():
            clock.observe(item.version)
        sha = repo.restore(ref, node=node, clock=clock)
    except GitError as exc:
        _fail(exc)
    if sha:
        typer.echo(sha)
    else:
        typer.echo("nothing to commit")


@app.command()
def pause() -> None:
    config = _load(False)
    RuntimeState(config.state_dir / "state.json").set_paused(True)
    typer.echo("paused")


@app.command()
def resume() -> None:
    config = _load(False)
    RuntimeState(config.state_dir / "state.json").set_paused(False)
    typer.echo("resumed")


@app.command()
def events(follow: bool = typer.Option(False, "-f", "--follow")) -> None:
    config = _load(False)
    EventLog(config.state_dir / "events.jsonl").tail(follow)


@app.command()
def sync(
    path: str | None = typer.Option(None, "--path"),
    peer: str | None = typer.Option(None, "--peer"),
    force: bool = typer.Option(False, "--force"),
    no_hooks: bool = typer.Option(False, "--no-hooks"),
) -> None:
    _ = (path, peer, force, no_hooks)
    typer.secho("network layer is not implemented yet (M2+)", fg="yellow", err=True)
    raise typer.Exit(2)


@app.command()
def peers(watch: bool = typer.Option(False, "--watch")) -> None:
    while True:
        config = _load(False)
        items = _read_peers(config)
        if not items:
            typer.echo("no peers")
        else:
            typer.echo(
                f"{'NODE':<38} {'ROLE':<6} {'TAGS':<20} {'ADDRESS':<24} LAST SEEN"
            )
            now = time.time()
            for item in items:
                node_id = str(item.get("node_id", "?"))
                role = str(item.get("role", "?"))
                tags = ",".join(str(tag) for tag in item.get("tags", [])) or "-"
                address = str(item.get("address", "?"))
                last_seen = item.get("last_seen")
                age = (
                    f"{int(now - last_seen)}s ago"
                    if isinstance(last_seen, (int, float))
                    else "?"
                )
                typer.echo(
                    f"{node_id:<38} {role:<6} {tags:<20} {address:<24} {age}"
                )
        if not watch:
            return
        time.sleep(2)


@app.command()
def gc() -> None:
    config = _load(False)
    repo = GitRepo(config.repo_dir)
    try:
        repo.ensure()
        output = repo.gc()
    except GitError as exc:
        _fail(exc)
    if output.strip():
        typer.echo(output.strip())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
