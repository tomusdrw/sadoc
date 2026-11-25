"""Configuration loading for sadoc."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

# Everything sadoc owns lives under one root: the configuration, the secrets,
# the state database, the logs, the reports, the repository checkouts and the
# tool caches. One folder to inspect, back up or wipe, rather than a piece in
# .config, a piece in .local/share and a piece in .cache.
HOME = Path.home() / ".sadoc"
DEFAULT_CONFIG = HOME / "config.toml"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_WALL_CLOCK = 1800
DEFAULT_PROMPT_INACTIVITY = 600
# Pi's provider request timer takes a signed 32-bit millisecond delay.
MAX_PROMPT_INACTIVITY = (2**31 - 1) // 1000


class ConfigError(Exception):
    """The configuration says something that cannot be honoured."""


@dataclass
class ModelSpec:
    provider: str = "local"
    model: str = "local-model"
    base_url: str = "http://127.0.0.1:8080/v1"
    context: int = 262144
    max_tokens: int = 16384
    reasoning: bool = True
    reasoning_effort: int | float | str | None = None


@dataclass(frozen=True)
class Timeouts:
    # Omitted overrides let each prompt keep its existing budget.
    prompt_wall_clock: int | None = None
    prompt_inactivity: int | None = None
    note_generation: int = 900

    @property
    def wall_clock(self) -> int:
        return self.prompt_wall_clock or DEFAULT_PROMPT_WALL_CLOCK

    @property
    def inactivity(self) -> int:
        return self.prompt_inactivity or DEFAULT_PROMPT_INACTIVITY


@dataclass
class Config:
    owner: str = ""
    github_app_id: int | None = None
    owners: list[str] = field(default_factory=list)
    state_db: Path = HOME / "state.db"
    workdir: Path = HOME / "work"
    agent_dir: Path = HOME / "pi-agent"
    report_dir: Path = HOME / "reports"

    # schedule
    window_start: str = "01:00"
    window_end: str = "07:00"
    max_prs_per_night: int = 5
    max_attempts: int = 3
    max_repos_per_night: int = 0  # 0 = unlimited (bounded by max_prs_per_night)

    # scope
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    skip_forks: bool = True
    max_inactive_days: int = 365
    min_outdated_count: int = 1

    # merge
    auto_merge_tiers: list[str] = field(default_factory=lambda: ["A"])
    require_checks: bool = True
    require_no_review_requests: bool = True
    require_no_unresolved_threads: bool = True

    # sandbox
    allow_network: bool = True
    scrub_env: bool = True

    # behaviour
    dry_run: bool = False
    tier_b_major: bool = True
    tier_c_security: bool = True

    # notifications
    notify: dict = field(
        default_factory=lambda: {
            "enabled": False,
            "backend": "none",
            "review_reminder_hours": 24,
            "merge_reminder_hours": 48,
        }
    )

    # the voice Sadoc writes in
    persona: dict = field(default_factory=dict)

    # rootless containers, for test environments that would otherwise be
    # installed onto the machine
    allow_containers: bool = False
    container_runtime: str = "podman"

    # stop Dependabot competing with this tool in the repositories it handles
    disable_dependabot: bool = True

    models: dict[str, ModelSpec] = field(default_factory=dict)
    default_model: ModelSpec = field(default_factory=ModelSpec)

    # per-repo overrides: {"repo": {"verify": "...", "skip": bool, ...}}
    repos: dict[str, dict] = field(default_factory=dict)
    timeouts: Timeouts = field(default_factory=Timeouts)

    @property
    def base_model(self) -> ModelSpec:
        return self.models.get("base", self.default_model)

    def model_for(self, task: str) -> ModelSpec:
        return self.models.get(task, self.base_model)


def _model_from(
    data: dict, section: str, default_effort: int | float | str | None = None
) -> ModelSpec:
    effort = data.get("reasoning_effort", default_effort)
    levels = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    if effort is not None and not (
        (type(effort) in (int, float) and 0 <= effort <= 0.99) or effort in levels
    ):
        raise ConfigError(
            f"[{section}] reasoning_effort must be a number from 0 to 0.99 "
            f"or one of: {', '.join(levels)}"
        )
    return ModelSpec(
        provider=data.get("provider", "local"),
        model=data.get("model", "local-model"),
        base_url=data.get("base_url", "http://127.0.0.1:8080/v1"),
        context=int(data.get("context", 262144)),
        max_tokens=int(data.get("max_tokens", 16384)),
        reasoning=bool(data.get("reasoning", True)),
        reasoning_effort=effort,
    )


def _timeouts_from(data: object) -> Timeouts:
    if not isinstance(data, dict):
        raise ConfigError("timeouts must be a TOML table")
    values = {}
    for key in ("prompt_wall_clock", "prompt_inactivity", "note_generation"):
        if key in data:
            value = data[key]
            if type(value) is not int or value <= 0:
                raise ConfigError(f"timeouts.{key} must be a positive integer (seconds)")
            values[key] = value
    limits = Timeouts(**values)
    if limits.inactivity > MAX_PROMPT_INACTIVITY:
        raise ConfigError(f"timeouts.prompt_inactivity must be at most {MAX_PROMPT_INACTIVITY}s")
    if limits.inactivity >= limits.wall_clock:
        raise ConfigError("timeouts.prompt_inactivity must be less than timeouts.prompt_wall_clock")
    return limits


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    cfg = Config()
    if not path.exists():
        return cfg
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc

    cfg.timeouts = _timeouts_from(raw.get("timeouts", {}))
    cfg.owner = raw.get("owner", cfg.owner)
    cfg.owners = list(raw.get("owners", []) or [])
    github = raw.get("github", {})
    mode = github.get("mode", "gh")
    if mode not in ("gh", "app"):
        raise ConfigError("github.mode must be 'gh' or 'app'")
    if mode == "app":
        app_id = github.get("app_id")
        if type(app_id) is not int or app_id <= 0:
            raise ConfigError("github.app_id must be a positive integer")
        if not cfg.owner or any(owner.lower() != cfg.owner.lower() for owner in cfg.owners):
            raise ConfigError("GitHub App mode requires one owner")
        cfg.github_app_id = app_id

    cfg.dry_run = bool(raw.get("dry_run", os.environ.get("SADOC_DRY_RUN") == "1"))

    for key in ("state_db", "workdir", "agent_dir", "report_dir"):
        if key in raw:
            setattr(cfg, key, Path(os.path.expanduser(raw[key])))

    sched = raw.get("schedule", {})
    cfg.window_start = sched.get("window", "01:00-07:00").split("-")[0]
    cfg.window_end = sched.get("window", "01:00-07:00").split("-")[-1]
    cfg.max_prs_per_night = int(sched.get("max_prs_per_night", cfg.max_prs_per_night))
    cfg.max_attempts = int(sched.get("max_attempts", cfg.max_attempts))
    cfg.max_repos_per_night = int(sched.get("max_repos_per_night", 0))

    scope = raw.get("scope", {})
    cfg.include = list(scope.get("include", []))
    cfg.exclude = list(scope.get("exclude", []))
    cfg.skip_forks = bool(scope.get("skip_forks", True))
    cfg.max_inactive_days = int(scope.get("max_inactive_days", cfg.max_inactive_days))
    cfg.min_outdated_count = int(scope.get("min_outdated_count", 1))

    merge = raw.get("merge", {})
    cfg.auto_merge_tiers = list(merge.get("auto_merge_tier", cfg.auto_merge_tiers))
    cfg.require_checks = bool(merge.get("require_checks", True))
    cfg.require_no_review_requests = bool(merge.get("require_no_review_requests", True))
    cfg.require_no_unresolved_threads = bool(merge.get("require_no_unresolved_threads", True))

    sandbox = raw.get("sandbox", {})
    cfg.allow_network = bool(sandbox.get("allow_network", True))
    cfg.scrub_env = bool(sandbox.get("scrub_env", True))
    # Rootless containers, for test environments that would otherwise be
    # installed onto the machine. These live under [sandbox] with the rest of
    # the sandbox settings, but the top level is accepted too.
    cfg.allow_containers = bool(sandbox.get("allow_containers", raw.get("allow_containers", False)))
    runtime = str(sandbox.get("container_runtime", raw.get("container_runtime", "podman")))
    if runtime.lower() == "docker":
        # The Docker socket is root-equivalent: reaching the daemon is reaching
        # root. Sadoc only ever uses a rootless runtime.
        raise ConfigError(
            "container_runtime must not be 'docker'; use a rootless runtime such as podman"
        )
    cfg.container_runtime = runtime

    behaviour = raw.get("behaviour", {})
    cfg.tier_b_major = bool(behaviour.get("tier_b_major", True))
    cfg.tier_c_security = bool(behaviour.get("tier_c_security", True))

    cfg.persona = dict(raw.get("persona", {}) or {})
    cfg.disable_dependabot = bool(raw.get("disable_dependabot", True))

    cfg.notify = dict(raw.get("notify", {}) or {})
    cfg.notify.setdefault("enabled", False)
    cfg.notify.setdefault("backend", "none")
    cfg.notify.setdefault("review_reminder_hours", 24)
    cfg.notify.setdefault("merge_reminder_hours", 48)
    # Env overrides, handy for testing without sending real mail.
    override = os.environ.get("SADOC_NOTIFY_BACKEND")
    if override:
        cfg.notify["backend"] = override
    if os.environ.get("SADOC_NOTIFY_ENABLED") in ("0", "false", "no"):
        cfg.notify["enabled"] = False

    model_raw = raw.get("model", {})
    cfg.default_model = _model_from(model_raw, "model")
    for name, spec in (raw.get("models") or {}).items():
        model = _model_from(spec, f"models.{name}", cfg.default_model.reasoning_effort)
        if spec.keys() == {"reasoning_effort"}:
            model = replace(cfg.default_model, reasoning_effort=model.reasoning_effort)
        cfg.models[name] = model
    cfg.models.setdefault("base", cfg.default_model)

    cfg.repos = raw.get("repos", {}) or {}
    return cfg
