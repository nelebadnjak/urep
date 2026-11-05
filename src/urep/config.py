from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SecretStr

from .paths import PathError, is_forbidden, is_under, normalize


class ConfigError(ValueError):
    pass


_DURATION = re.compile(r"^(\d+)(ms|s|m|h)$")
_SIZE = re.compile(r"^(\d+)(b|kib|mib|gib)$", re.IGNORECASE)
_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def parse_duration_ms(value: str) -> int:
    match = _DURATION.match(value.strip().lower())
    if not match:
        raise ConfigError(f"invalid duration: {value!r} (expected e.g. 200ms, 2s, 5m)")
    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}
    return amount * multipliers[unit]


def parse_duration_s(value: str) -> int:
    return parse_duration_ms(value) // 1000


def parse_size(value: str) -> int:
    match = _SIZE.match(value.strip())
    if not match:
        raise ConfigError(f"invalid size: {value!r} (expected e.g. 16MiB)")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    multipliers = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
    return amount * multipliers[unit]


def parse_mode(value: str) -> int:
    try:
        mode = int(value.strip(), 8)
    except ValueError as exc:
        raise ConfigError(f"invalid mode: {value!r} (expected octal, e.g. 0755)") from exc
    if not 0 <= mode <= 0o7777:
        raise ConfigError(f"mode out of range: {value!r}")
    return mode


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _lines(value: str | None) -> list[str]:
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean for {name}: {raw!r}")


def _read_token_file(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read token file {path}: {exc}") from exc
    if not value:
        raise ConfigError(f"token file is empty: {path}")
    return value


def _dedupe(items: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    output: list[Any] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            output.append(item)
    return output


def _validate_hostport(value: str, what: str) -> None:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ConfigError(f"invalid {what}: {value!r} (expected host:port)")


def sanitize_prefix(value: str) -> str:
    text = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    if not text or not _PREFIX.match(text):
        raise ConfigError(f"invalid prefix: {value!r}")
    return text


def make_node_id(prefix: str | None = None) -> str:
    uid = str(uuid.uuid4())
    return f"{sanitize_prefix(prefix)}-{uid}" if prefix else uid


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: SecretStr
    tags: list[str]
    role: Literal["peer", "slave"]
    paths: list[Path]
    data_dir: Path
    state_dir: Path
    repo_dir: Path
    accept_roots: list[Path]
    listen: str
    multicast_group: str
    seeds: list[str]
    pre_hooks: list[str]
    post_hooks: list[str]
    hooks_enabled: bool
    debounce_ms: int
    anti_entropy_s: int
    max_clock_skew_s: int
    conflict_window_ms: int
    max_file_size: int
    dir_mode: int
    log_level: str
    prefix: str | None

    @classmethod
    def load(cls, cli: dict[str, Any] | None = None, require_token: bool = True) -> Config:
        values = cli or {}

        # token: CLI > CLI file > env > env file
        token: str
        if values.get("token"):
            token = str(values["token"])
        elif values.get("token_file"):
            token = _read_token_file(Path(values["token_file"]))
        elif os.environ.get("UREP_TOKEN"):
            token = os.environ["UREP_TOKEN"]
        elif os.environ.get("UREP_TOKEN_FILE"):
            token = _read_token_file(Path(os.environ["UREP_TOKEN_FILE"]))
        elif require_token:
            raise ConfigError(
                "token is required (--token, --token-file, UREP_TOKEN, UREP_TOKEN_FILE)"
            )
        else:
            token = ""

        raw_paths = values.get("paths")
        if raw_paths is None:
            raw_paths = _csv(os.environ.get("UREP_PATH")) or []
        try:
            paths = _dedupe([normalize(path) for path in raw_paths])
            data_dir = normalize(os.environ.get("UREP_DATA_DIR", "/urep"))
            state_dir = normalize(os.environ.get("UREP_STATE_DIR", "/var/lib/urep"))
        except PathError as exc:
            raise ConfigError(str(exc)) from exc
        repo_dir = state_dir / "repo"

        raw_roots = values.get("roots")
        if raw_roots is None:
            env_roots = _csv(os.environ.get("UREP_ACCEPT_ROOTS"))
            raw_roots = env_roots
        try:
            if raw_roots is None:
                roots = _dedupe([data_dir, *(path.parent for path in paths)])
            else:
                roots = _dedupe([normalize(root) for root in raw_roots])
        except PathError as exc:
            raise ConfigError(str(exc)) from exc

        raw_tags = values.get("tags")
        if raw_tags is None:
            env_tag = os.environ.get("UREP_TAG")
            raw_tags = _csv(env_tag) if env_tag is not None else ["default"]
        raw_tags = list(raw_tags or [])
        tags = _dedupe([tag.strip().lower() for tag in raw_tags if tag.strip()])
        for tag in tags:
            if tag != "*" and not _TAG.match(tag):
                raise ConfigError(f"invalid tag: {tag!r}")
        if "*" in tags and len(tags) > 1:
            raise ConfigError("'*' must be the only tag")

        if values.get("slave"):
            role: str = "slave"
        else:
            role = (os.environ.get("UREP_ROLE") or "peer").strip().lower()
        if role not in {"peer", "slave"}:
            raise ConfigError(f"invalid role: {role!r} (peer or slave)")

        listen = values.get("listen") or os.environ.get("UREP_LISTEN") or "0.0.0.0:7801"
        _validate_hostport(listen, "listen address")
        multicast_group = os.environ.get("UREP_MULTICAST_GROUP") or "239.255.42.99:7802"
        _validate_hostport(multicast_group, "multicast group")

        pre_hooks = values.get("pre_hooks")
        if pre_hooks is None:
            pre_hooks = _lines(os.environ.get("UREP_PRE_HOOK"))
        post_hooks = values.get("post_hooks")
        if post_hooks is None:
            post_hooks = _lines(os.environ.get("UREP_POST_HOOK"))

        log_level = (values.get("log_level") or os.environ.get("UREP_LOG_LEVEL") or "info").lower()
        prefix_raw = values.get("prefix") or os.environ.get("UREP_PREFIX")
        prefix = sanitize_prefix(prefix_raw) if prefix_raw else None

        config = cls(
            token=SecretStr(token),
            tags=tags,
            role=role,  # type: ignore[arg-type]
            paths=paths,
            data_dir=data_dir,
            state_dir=state_dir,
            repo_dir=repo_dir,
            accept_roots=roots,
            listen=listen,
            multicast_group=multicast_group,
            seeds=_csv(os.environ.get("UREP_SEEDS")) or [],
            pre_hooks=pre_hooks,
            post_hooks=post_hooks,
            hooks_enabled=_env_bool("UREP_HOOKS_ENABLED", True),
            debounce_ms=parse_duration_ms(os.environ.get("UREP_DEBOUNCE", "200ms")),
            anti_entropy_s=parse_duration_s(os.environ.get("UREP_ANTI_ENTROPY", "60s")),
            max_clock_skew_s=parse_duration_s(os.environ.get("UREP_MAX_CLOCK_SKEW", "5m")),
            conflict_window_ms=parse_duration_ms(os.environ.get("UREP_CONFLICT_WINDOW", "2s")),
            max_file_size=parse_size(os.environ.get("UREP_MAX_FILE_SIZE", "16MiB")),
            dir_mode=parse_mode(os.environ.get("UREP_DIR_MODE", "0755")),
            log_level=log_level,
            prefix=prefix,
        )
        config.validate_paths()
        return config

    def validate_paths(self) -> None:
        for path in self.paths:
            if is_forbidden(path, [self.repo_dir, self.state_dir]):
                raise ConfigError(f"path is forbidden: {path}")
            if not is_under(path, self.accept_roots):
                raise ConfigError(f"path is outside UREP_ACCEPT_ROOTS: {path}")
            if path.exists() and path.is_dir():
                raise ConfigError(f"only individual files are supported: {path}")
        if self.paths and not self.tags:
            raise ConfigError("node tags are empty; cannot tag outgoing paths")

    def redacted(self) -> dict[str, Any]:
        return {
            "token": "****" if self.token.get_secret_value() else "",
            "tags": self.tags,
            "role": self.role,
            "paths": [str(path) for path in self.paths],
            "data_dir": str(self.data_dir),
            "state_dir": str(self.state_dir),
            "repo_dir": str(self.repo_dir),
            "accept_roots": [str(root) for root in self.accept_roots],
            "listen": self.listen,
            "multicast_group": self.multicast_group,
            "seeds": self.seeds,
            "pre_hooks": self.pre_hooks,
            "post_hooks": self.post_hooks,
            "hooks_enabled": self.hooks_enabled,
            "debounce_ms": self.debounce_ms,
            "anti_entropy_s": self.anti_entropy_s,
            "max_clock_skew_s": self.max_clock_skew_s,
            "conflict_window_ms": self.conflict_window_ms,
            "max_file_size": self.max_file_size,
            "dir_mode": f"{self.dir_mode:04o}",
            "log_level": self.log_level,
            "prefix": self.prefix,
        }
