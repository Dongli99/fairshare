"""The routing rule. Pure: accounts + snapshots + now in, ranked verdicts out. No I/O."""
from __future__ import annotations

from dataclasses import dataclass

from .model import Account, Snapshot, Tuning

HOUR = 3600.0


@dataclass(frozen=True)
class Verdict:
    account: Account
    eligible: bool
    score: float                    # budget units to burn per hour to use the long window fully
    short_reset_in_h: float | None  # None = short window not running
    reason: str


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

    hot = short_used >= t.short_hot_pct and (short_in or 0) > t.grace_hours
    exhausted = long_used >= t.long_exhausted_pct
    if exhausted:
        why = f"week {long_used:.0f}% used, resets in {long_in:.0f}h"
    elif hot:
        why = f"5h window {short_used:.0f}% used, resets in {short_in:.1f}h"
    elif snap is None:
        why = "no usage data yet, assumed fresh"
    else:
        why = f"week {long_used:.0f}% used, resets in {long_in:.0f}h"
    return Verdict(acct, not (hot or exhausted), score, short_in, why)


def rank(accounts: list[Account], snaps: dict[str, Snapshot], now: float,
         t: Tuning = Tuning()) -> list[Verdict]:
    """Best first. Eligible accounts by score; if none is eligible, whichever frees first."""
    vs = [evaluate(a, snaps.get(a.id), now, t) for a in accounts]
    ok = sorted((v for v in vs if v.eligible), key=lambda v: (-v.score, v.account.id))
    rest = sorted((v for v in vs if not v.eligible),
                  key=lambda v: (v.short_reset_in_h if v.short_reset_in_h is not None else 1e9,
                                 v.account.id))
    return ok + rest
