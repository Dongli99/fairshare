"""Provider-neutral data model. Nothing here knows about any one provider."""
from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import browser


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
    short_hours: float | None = None   # its length, when the plan file gives it


@dataclass(frozen=True)
class Account:
    id: str
    provider: str
    plan: Plan | None              # None: the configured plan has no plan file (see plan_error)
    config_dir: Path
    status_args: tuple[str, ...] | None = None   # overrides the provider's sign-in check command
    usage_args: tuple[str, ...] | None = None    # overrides its usage probe; () turns it off
    capacity: float | None = None  # the account's own override; it survives plan detection
    plan_error: str | None = None  # why `plan` is None: the account is left out of ranking
    browser: str | None = None     # how its sign-in link opens (`browser.parse`); None: default


@dataclass(frozen=True)
class AccountStatus:
    """What the provider says about one account's sign-in. Held in memory only."""
    logged_in: bool
    usable: bool                  # a subscription login (the provider's own kind), not an API key
    identity: str | None = None   # opaque: equal identities share one quota
    plan: str | None = None       # a fairshare plan name, None when the provider's maps to none
    expires_at: float | None = None   # when the sign-in ends (unix seconds), when the provider says


# Why a usage probe read nothing: short and value-free. A provider may add a detail after
# USAGE_NO_ANSWER only (`no answer: timed out`); the CLI tells USAGE_MODEL_TURN apart (it spends
# usage, so it is said loudly).
USAGE_NO_ANSWER = "no answer"
USAGE_SIGNED_OUT = "not signed in"
USAGE_NOT_UNDERSTOOD = "not understood"
USAGE_MODEL_TURN = "ran as a model turn"

MODEL_WINDOW_SEP = ":"


def model_window(long_name: str, model: str) -> str:
    """The snapshot key of a per-model week (`seven_day:Fable`): kept beside the plan's windows,
    never read by the policy."""
    return f"{long_name}{MODEL_WINDOW_SEP}{model}"


def window_model(key: str) -> str | None:
    """The model a snapshot key names a week for, else None (a plan window)."""
    return key.partition(MODEL_WINDOW_SEP)[2] or None


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
    runs (`runner`, the argv before `capture`) and the user's own command it wraps, if any."""
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
    short = w.get("short", {})
    return Plan(d["name"], d["provider"], cap, w["long"]["name"], float(w["long"]["hours"]),
                short.get("name"), float(short["hours"]) if "hours" in short else None)


DEFAULT_HUB = "~/.local/share/fairshare/hub"   # where setup proposes the hub
ACCOUNT_ID = re.compile(r"\w[\w.-]*")         # an id is also a folder name in the hub


class ConfigError(ValueError):
    """accounts.toml cannot be used. The message says what is wrong and where; callers add the
    file's name."""


def _sole_provider() -> str | None:
    """The one provider the plan files serve, or None when there are several."""
    found = {_plan_data(n)["provider"] for n in plan_names()}
    return found.pop() if len(found) == 1 else None


def _account(a: dict, hub: Path) -> Account:
    if "config_dir" in a:
        raise ValueError("config_dir is no longer used: each account lives in the hub, at "
                         "<hub>/accounts/<id>; move this file aside, then run `fairshare setup`")
    if not isinstance(a["id"], str) or not ACCOUNT_ID.fullmatch(a["id"]):
        raise ValueError(f"id {a['id']!r}: use letters, digits, '.', '-' or '_'")
    capacity = None if a.get("capacity") is None else float(a["capacity"])
    try:
        plan, error = load_plan(a["plan"], capacity), None
    except FileNotFoundError:     # a renamed or removed plan file must not take the rest down
        plan, error = None, f"plan {a['plan']} not found; known: {', '.join(plan_names())}"
    provider = a.get("provider") or (plan.provider if plan else _sole_provider())
    if not provider:
        raise ValueError(f"{error}, and no provider is named")
    args, usage = a.get("status_args"), a.get("usage_args")
    if isinstance(usage, list) and not all(isinstance(x, str) for x in usage):
        raise ValueError("usage_args must be a string or a list of strings")
    if (spec := a.get("browser")) is not None:
        browser.parse(spec)                  # raises ValueError saying what is accepted
    return Account(a["id"], provider, plan, hub / "accounts" / a["id"],
                   tuple(shlex.split(args)) if args else None,
                   None if usage is None else tuple(usage if isinstance(usage, list)
                                                    else shlex.split(usage)),
                   capacity, error, spec)


def load_accounts(path: Path) -> tuple[list[Account], Tuning, Path]:
    """The accounts, the tuning and the hub folder. Raises ConfigError, never anything else, for
    a file that cannot be used. An account whose plan has no plan file loads with `plan` None and
    `plan_error` set."""
    try:
        d = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(e)) from None
    except (OSError, UnicodeDecodeError):
        raise ConfigError("cannot be read as text") from None
    if not isinstance(d.get("account", []), list):
        raise ConfigError("account must be a list of [[account]] tables")
    if not isinstance(d.get("hub", DEFAULT_HUB), str):
        raise ConfigError("hub must be a folder path in quotes")
    hub = Path(d.get("hub", DEFAULT_HUB)).expanduser()
    accounts = []
    for i, a in enumerate(d.get("account", []), 1):
        try:
            accounts.append(_account(a, hub))
        except KeyError as e:
            raise ConfigError(f"account {i}: missing {e}") from None
        except (TypeError, ValueError, AttributeError) as e:
            raise ConfigError(f"account {i}: {e}") from None
        if [x.id for x in accounts].count(accounts[-1].id) > 1:
            raise ConfigError(f"account {i}: id {accounts[-1].id!r} is used twice")
    policy, valid = d.get("policy", {}), ", ".join(Tuning.__dataclass_fields__)
    if not isinstance(policy, dict):
        raise ConfigError("[policy] must be a table")
    for k, v in policy.items():
        if k not in Tuning.__dataclass_fields__:
            raise ConfigError(f"[policy] unknown key {k!r}; valid keys: {valid}")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"[policy] {k} must be a number")
    return accounts, Tuning(**policy), hub


def plan_names() -> list[str]:
    return sorted(f.name[:-5] for f in resources.files("fairshare.plans").iterdir()
                  if f.name.endswith(".toml"))


# accounts.toml is edited as text: a change touches only its own lines, so the user's comments and
# layout survive, and every result is parsed again before it is returned.

_TABLE = re.compile(r"^\s*\[")
_HUB_LINE = re.compile(r"^\s*hub\s*=")
_ACCOUNT_TABLE = re.compile(r"^\s*\[\[\s*account\s*\]\]")


def account_table(entry: dict) -> str:
    """One [[account]] table. Refuses what would not load."""
    if not ACCOUNT_ID.fullmatch(entry["id"]):
        raise ValueError(f"account id {entry['id']!r}: use letters, digits, '.', '-' or '_'")
    if entry["plan"] not in plan_names():
        raise ValueError(f"unknown plan {entry['plan']!r} (known: {', '.join(plan_names())})")
    load_plan(entry["plan"], entry.get("capacity"))
    return "\n".join(["[[account]]", *(f"{k} = {_toml(v)}" for k, v in entry.items())]) + "\n"


def with_hub(text: str, hub: str) -> str:
    """`text` whose top-level `hub` key is `hub`: its one line replaced, or added at the top."""
    old = tomllib.loads(text)
    if old.get("hub") == hub:
        return text
    new = f"hub = {_toml(hub)}\n" + ("\n" + text if text.strip() else "")
    if "hub" in old:
        lines = text.splitlines(keepends=True)
        at = next((i for i, line in enumerate(lines) if _HUB_LINE.match(line)
                   and not any(_TABLE.match(x) for x in lines[:i])), None)
        if at is None:
            raise ValueError("could not find the hub line in accounts.toml")
        new = "".join(lines[:at] + [f"hub = {_toml(hub)}\n"] + lines[at + 1:])
    if tomllib.loads(new) != {**old, "hub": hub}:
        raise ValueError("could not set the hub in accounts.toml")
    return new


def with_account(text: str, entry: dict) -> str:
    """`text` with one more [[account]] table at its end."""
    old = tomllib.loads(text).get("account", [])
    if any(a.get("id") == entry["id"] for a in old):
        raise ValueError(f"account id {entry['id']!r} is already configured")
    body = text if not text or text.endswith("\n") else text + "\n"
    new = body + ("\n" if body.strip() else "") + account_table(entry)
    if tomllib.loads(new).get("account", []) != [*old, entry]:
        raise ValueError("could not add the account to accounts.toml")
    return new


def without_account(text: str, account_id: str) -> str:
    """`text` without the [[account]] table whose id is `account_id`: its lines, from its header
    to the next table, are removed and nothing else."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if _TABLE.match(line)] + [len(lines)]
    for i, end in zip(starts, starts[1:]):
        if _ACCOUNT_TABLE.match(lines[i]):
            try:
                found = tomllib.loads("".join(lines[i:end]))["account"][0].get("id")
            except (tomllib.TOMLDecodeError, KeyError, IndexError):
                continue
            if found == account_id:
                new = "".join(lines[:i] + lines[end:])
                old = tomllib.loads(text).get("account", [])
                if tomllib.loads(new).get("account", []) != [a for a in old
                                                             if a.get("id") != account_id]:
                    raise ValueError("could not remove the account from accounts.toml")
                return new
    raise ValueError(f"no account {account_id!r} in accounts.toml")


def _toml(v) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    raise ValueError(f"cannot write {v!r} to accounts.toml")
