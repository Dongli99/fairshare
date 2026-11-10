"""The routing rule. Pure: accounts + snapshots + now in, ranked verdicts out. No I/O."""
from __future__ import annotations

from dataclasses import dataclass

from .model import Account, Snapshot, Tuning

HOUR = 3600.0


@dataclass(frozen=True)
class Verdict:
    account: Account
    eligible: bool                  # False = near a limit; the CLI reports it, ranking ignores it
    score: float                    # budget units to burn per hour to use the long window fully,
                                    # scaled down while the short window is nearly full; an
                                    # exhausted week is -(hours until it resets), below any usable
                                    # account
    reason: str
    usable_in: float                # hours until it is eligible again; 0 when eligible now


def evaluate(acct: Account, snap: Snapshot | None, now: float, t: Tuning) -> Verdict:
    p = acct.plan
    wins = snap.windows if snap else {}
    short, long_ = wins.get(p.short_name), wins.get(p.long_name)
    # A window past its reset has rolled over: treat as unused.
    if short and short.resets_at <= now:
        short = None
    if long_ and long_.resets_at <= now:
        long_ = None

    short_used = short.used_pct if short else 0.0
    short_in = (short.resets_at - now) / HOUR if short else None
    long_used = long_.used_pct if long_ else 0.0
    long_in = (long_.resets_at - now) / HOUR if long_ else p.long_hours  # unknown = a fresh window
    score = p.capacity * (100.0 - long_used) / max(long_in, t.min_hours)

    # Near the short limit and not about to reset: only the unused part of that window is usable.
    hot = short_used >= t.short_hot_pct and (short_in or 0) >= t.grace_hours
    if hot:
        score *= (100.0 - short_used) / 100.0
    # A spent week is a fact, not a preference: nothing usable until it resets.
    exhausted = long_used >= t.long_exhausted_pct
    if exhausted:
        score = -long_in
    if long_ is None:
        why = f"no active {p.long_name} window, assumed fresh"
    elif hot and not exhausted:
        why = f"{p.short_name} {short_used:.0f}% used, resets in {short_in:.1f}h"
    else:
        why = f"{p.long_name} {long_used:.0f}% used, resets in {long_in:.0f}h"
    usable_in = max(short_in if hot else 0.0, long_in if exhausted else 0.0)
    return Verdict(acct, not (hot or exhausted), score, why, usable_in)


def rank(accounts: list[Account], snaps: dict[str, Snapshot], now: float,
         t: Tuning = Tuning()) -> list[Verdict]:
    """Best first: highest score, ties by id."""
    vs = [evaluate(a, snaps.get(a.id), now, t) for a in accounts]
    return sorted(vs, key=lambda v: (-v.score, v.account.id))
