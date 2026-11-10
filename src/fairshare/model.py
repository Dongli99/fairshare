"""Provider-neutral data model. Nothing here knows about any one provider."""
from __future__ import annotations

import json
import re
import shlex
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
    long_name: str       # provider's name for the long window
    long_hours: float    # its length: the fallback for an unknown reset
    short_name: str | None = None   # provider's name for the short window; None: the plan has none


@dataclass(frozen=True)
class Account:
    id: str
    provider: str
    plan: Plan | None              # None: the configured plan has no plan file (see plan_error)
    config_dir: Path
    status_args: tuple[str, ...] | None = None   # overrides the provider's sign-in check command
    capacity: float | None = None  # the account's own override; it survives plan detection
    plan_error: str | None = None  # why `plan` is None: the account is left out of ranking


@dataclass(frozen=True)
class AccountStatus:
    """What the provider says about one account's sign-in. Held in memory only."""
    logged_in: bool
    usable: bool                  # a subscription login (the provider's own kind), not an API key
    identity: str | None = None   # opaque: equal identities share one quota
    plan: str | None = None       # a fairshare plan name, None when the provider's maps to none


@dataclass(frozen=True)
class Candidate:
    """An account found on disk, before the user confirms it."""
    id: str
    provider: str
    config_dir: Path
    found: str           # where it was found, shown to the user


@dataclass(frozen=True)
class Capture:
    """A usage hook the provider calls, as found in its settings: which fairshare command it
    runs (`runner`, the argv before `capture`), for which account, and the user's own command it
    wraps, if any."""
    account: str
    runner: tuple[str, ...]
    then: str | None


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


def _plan_data(name: str) -> dict:
    return tomllib.loads((resources.files("fairshare.plans") / f"{name}.toml").read_text())


def plan_for(provider: str, native: str | None) -> str | None:
    """The plan whose `names` list the provider's own name for it, else None."""
    for name in plan_names():
        d = _plan_data(name)
        if d["provider"] == provider and native in d.get("names", []):
            return d["name"]
    return None


def window_names(provider: str) -> set[str]:
    """Every window name the plan files give for `provider`: what its usage payload may carry."""
    return {w["name"] for n in plan_names() if (d := _plan_data(n))["provider"] == provider
            for w in d["windows"].values()}


def load_plan(name: str, capacity: float | None = None) -> Plan:
    d = _plan_data(name)
    w = d["windows"]
    cap = float(d["capacity"]) if capacity is None else float(capacity)
    if cap <= 0:
        raise ValueError(f"capacity must be positive (got {cap}) for plan {name}")
    return Plan(d["name"], d["provider"], cap, w["long"]["name"], float(w["long"]["hours"]),
                w["short"]["name"] if "short" in w else None)


class ConfigError(ValueError):
    """accounts.toml cannot be used. The message says what is wrong and where; callers add the
    file's name."""


def _sole_provider() -> str | None:
    """The one provider the plan files serve, or None when there are several."""
    found = {_plan_data(n)["provider"] for n in plan_names()}
    return found.pop() if len(found) == 1 else None


def _account(a: dict) -> Account:
    capacity = None if a.get("capacity") is None else float(a["capacity"])
    try:
        plan, error = load_plan(a["plan"], capacity), None
    except FileNotFoundError:     # a renamed or removed plan file must not take the rest down
        plan, error = None, f"plan {a['plan']} not found; known: {', '.join(plan_names())}"
    provider = a.get("provider") or (plan.provider if plan else _sole_provider())
    if not provider:
        raise ValueError(f"{error}, and no provider is named")
    args = a.get("status_args")
    return Account(a["id"], provider, plan, Path(a["config_dir"]).expanduser(),
                   tuple(shlex.split(args)) if args else None, capacity, error)


def load_accounts(path: Path) -> tuple[list[Account], Tuning]:
    """Raises ConfigError, never anything else, for a file that cannot be used. An account whose
    plan has no plan file loads with `plan` None and `plan_error` set."""
    try:
        d = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(e)) from None
    except (OSError, UnicodeDecodeError):
        raise ConfigError("cannot be read as text") from None
    if not isinstance(d.get("account", []), list):
        raise ConfigError("account must be a list of [[account]] tables")
    accounts = []
    for i, a in enumerate(d.get("account", []), 1):
        try:
            accounts.append(_account(a))
        except KeyError as e:
            raise ConfigError(f"account {i}: missing {e}") from None
        except (TypeError, ValueError, AttributeError) as e:
            raise ConfigError(f"account {i}: {e}") from None
    policy, valid = d.get("policy", {}), ", ".join(Tuning.__dataclass_fields__)
    if not isinstance(policy, dict):
        raise ConfigError("[policy] must be a table")
    for k, v in policy.items():
        if k not in Tuning.__dataclass_fields__:
            raise ConfigError(f"[policy] unknown key {k!r}; valid keys: {valid}")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"[policy] {k} must be a number")
    return accounts, Tuning(**policy)


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
