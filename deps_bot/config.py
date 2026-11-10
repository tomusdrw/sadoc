"""Configuration loading for deps-bot."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "deps-bot" / "config.toml"
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ModelSpec:
    provider: str = "local"
    model: str = "local-model"
    base_url: str = "http://127.0.0.1:8080/v1"
    context: int = 262144
    max_tokens: int = 16384
    reasoning: bool = True


@dataclass
class Config:
    owner: str = ""
    owners: list[str] = field(default_factory=list)
    state_db: Path = Path.home() / ".local" / "share" / "deps-bot" / "state.db"
    workdir: Path = Path.home() / ".cache" / "deps-bot" / "work"
    agent_dir: Path = Path.home() / ".cache" / "deps-bot" / "pi-agent"
    report_dir: Path = Path.home() / ".local" / "share" / "deps-bot" / "reports"

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

    models: dict[str, ModelSpec] = field(default_factory=dict)
    default_model: ModelSpec = field(default_factory=ModelSpec)

    # per-repo overrides: {"repo": {"verify": "...", "skip": bool, ...}}
    repos: dict[str, dict] = field(default_factory=dict)

    @property
    def base_model(self) -> ModelSpec:
        return self.models.get("base", self.default_model)

    def model_for(self, task: str) -> ModelSpec:
        return self.models.get(task, self.base_model)


def _model_from(data: dict) -> ModelSpec:
    return ModelSpec(
        provider=data.get("provider", "local"),
        model=data.get("model", "local-model"),
        base_url=data.get("base_url", "http://127.0.0.1:8080/v1"),
        context=int(data.get("context", 262144)),
        max_tokens=int(data.get("max_tokens", 16384)),
        reasoning=bool(data.get("reasoning", True)),
    )


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    cfg = Config()
    if not path.exists():
        return cfg
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg.owner = raw.get("owner", cfg.owner)
    cfg.owners = list(raw.get("owners", []) or [])
    cfg.dry_run = bool(raw.get("dry_run", os.environ.get("DEPSBOT_DRY_RUN") == "1"))

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

    behaviour = raw.get("behaviour", {})
    cfg.tier_b_major = bool(behaviour.get("tier_b_major", True))
    cfg.tier_c_security = bool(behaviour.get("tier_c_security", True))

    cfg.notify = dict(raw.get("notify", {}) or {})
    cfg.notify.setdefault("enabled", False)
    cfg.notify.setdefault("backend", "none")
    cfg.notify.setdefault("review_reminder_hours", 24)
    cfg.notify.setdefault("merge_reminder_hours", 48)
    # Env overrides, handy for testing without sending real mail.
    override = os.environ.get("DEPSBOT_NOTIFY_BACKEND")
    if override:
        cfg.notify["backend"] = override
    if os.environ.get("DEPSBOT_NOTIFY_ENABLED") in ("0", "false", "no"):
        cfg.notify["enabled"] = False

    model_raw = raw.get("model", {})
    cfg.default_model = _model_from(model_raw)
    for name, spec in (raw.get("models") or {}).items():
        cfg.models[name] = _model_from(spec)
    cfg.models.setdefault("base", cfg.default_model)

    cfg.repos = raw.get("repos", {}) or {}
    return cfg
