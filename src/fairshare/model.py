"""Provider-neutral data model. Nothing here knows about any one provider."""
from __future__ import annotations

import json
import re
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
class Candidate:
    """An account found on disk, before the user confirms it."""
    id: str
    provider: str
    config_dir: Path
    found: str           # where it was found, shown to the user


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
    cap = float(d["capacity"]) if capacity is None else float(capacity)
    if cap <= 0:
        raise ValueError(f"capacity must be positive (got {cap}) for plan {name}")
    return Plan(d["name"], d["provider"], cap,
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


def plan_names() -> list[str]:
    return sorted(f.name[:-5] for f in resources.files("fairshare.plans").iterdir()
                  if f.name.endswith(".toml"))


def render_config(accounts: list[dict], policy: dict) -> str:
    """accounts.toml text, the inverse of load_accounts. Refuses what would load wrong."""
    ids = [a["id"] for a in accounts]
    for a in accounts:
        if not re.fullmatch(r"\w[\w.-]*", a["id"]):
            raise ValueError(f"account id {a['id']!r}: use letters, digits, '.', '-' or '_'")
        if ids.count(a["id"]) > 1:
            raise ValueError(f"account id {a['id']!r} is used twice")
        if a["plan"] not in plan_names():
            raise ValueError(f"unknown plan {a['plan']!r} (known: {', '.join(plan_names())})")
        load_plan(a["plan"], a.get("capacity"))
    tables = ([("[policy]", policy)] if policy else []) + [("[[account]]", a) for a in accounts]
    out = []
    for header, table in tables:
        out += [header, *(f"{k} = {_toml(v)}" for k, v in table.items()), ""]
    return "\n".join(out)


def _toml(v) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    raise ValueError(f"cannot write {v!r} to accounts.toml")
