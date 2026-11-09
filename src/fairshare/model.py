"""Provider-neutral data model. Nothing here knows about any one provider."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Window:
    """One usage window as the provider reports it."""
    used_pct: float      # 0-100
    resets_at: float     # unix seconds


@dataclass(frozen=True)
class Plan:
    name: str
    provider: str
    capacity: float      # relative budget size; Pro = 1.0
    short_name: str      # provider's name for the short window
    short_hours: float
    long_name: str       # provider's name for the long window
    long_hours: float


@dataclass(frozen=True)
class Account:
    id: str
    provider: str
    plan: Plan
    config_dir: Path


@dataclass(frozen=True)
class Snapshot:
    captured_at: float
    windows: dict[str, Window] = field(default_factory=dict)


@dataclass(frozen=True)
class Tuning:
    short_hot_pct: float = 85.0       # a short window this full counts as "hot"
    long_exhausted_pct: float = 98.0  # a long window this full cannot take work
    grace_hours: float = 1.0          # a hot account resetting this soon is still usable
    min_hours: float = 1.0            # floor on time-to-reset, so a near reset stays finite


def load_plan(name: str, capacity: float | None = None) -> Plan:
    text = (resources.files("fairshare.plans") / f"{name}.toml").read_text()
    d = tomllib.loads(text)
    w = d["windows"]
    return Plan(d["name"], d["provider"], capacity or float(d["capacity"]),
                w["short"]["name"], float(w["short"]["hours"]),
                w["long"]["name"], float(w["long"]["hours"]))


def load_accounts(path: Path) -> tuple[list[Account], Tuning]:
    d = tomllib.loads(path.read_text())
    accounts = []
    for a in d.get("account", []):
        plan = load_plan(a["plan"], a.get("capacity"))
        accounts.append(Account(a["id"], a.get("provider", plan.provider), plan,
                                Path(a["config_dir"]).expanduser()))
    return accounts, Tuning(**d.get("policy", {}))
