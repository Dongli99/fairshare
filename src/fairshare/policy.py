"""The routing rule. Pure: accounts + snapshots + now in, ranked verdicts out. No I/O."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .model import Account, Snapshot, Tuning

HOUR = 3600.0


@dataclass(frozen=True)
class Verdict:
    account: Account
    eligible: bool                  # False = near or at a limit; the CLI reports it, ranking
                                    # ignores it
    score: float                    # budget units to burn per hour to use the long window fully,
                                    # scaled down while the short window is nearly full; a spent
                                    # window (week or short) is -(hours until the later of them
                                    # resets), below any usable account
    reason: str
    usable_in: float                # hours until it is eligible again; 0 when eligible now


def _dur(hours: float, digits: int = 0) -> str:
    """A time to reset for people: minutes under an hour, else hours."""
    return f"{max(0, round(hours * 60))}m" if hours < 1 else f"{hours:.{digits}f}h"


def evaluate(acct: Account, snap: Snapshot | None, now: float, t: Tuning,
             extra: float = 0.0) -> Verdict:
    """`extra`: percent of the long window the user will refill before it resets (a planned
    reset), counted as room left; a spent window stays spent until the refill shows."""
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
    score = p.capacity * (100.0 - long_used + extra) / max(long_in, t.min_hours)

    # A spent window is a fact, not a preference: nothing usable until it resets. The line is
    # the week's for both: a short window that full takes no work, however soon it resets.
    exhausted = long_used >= t.long_exhausted_pct
    spent = short_used >= t.long_exhausted_pct
    # Nearly full, not spent and not about to reset: only the unused part of that window is usable.
    hot = not spent and short_used >= t.short_hot_pct and (short_in or 0) >= t.grace_hours
    if hot:
        score *= (100.0 - short_used) / 100.0
    if exhausted or spent:
        score = -max(long_in if exhausted else 0.0, short_in if spent else 0.0)
    if long_ is None and not spent:
        why = f"no active {p.long_name} window, assumed fresh"
    elif (hot or spent) and not exhausted:
        why = f"{p.short_name} {short_used:.0f}% used, resets in {_dur(short_in, 1)}"
    else:
        why = f"{p.long_name} {long_used:.0f}% used, resets in {_dur(long_in)}"
    if extra:   # the refill is not there yet: say what is, for a job bigger than that
        why += f", +{extra:.0f}% planned ({max(0.0, 100.0 - long_used):.0f}% left until it is used)"
    # A hot short window stops counting once its reset is within the grace period; a spent one
    # counts until it resets.
    usable_in = max(short_in - t.grace_hours if hot else 0.0, short_in if spent else 0.0,
                    long_in if exhausted else 0.0)
    return Verdict(acct, not (hot or spent or exhausted), score, why, usable_in)


def rank(accounts: list[Account], snaps: dict[str, Snapshot], now: float,
         t: Tuning = Tuning(), extra: dict[str, float] | None = None) -> list[Verdict]:
    """Best first: highest score, ties by id. The grace hour holds only while no account is
    eligible without it: what is left of a nearly full window lasts minutes, so a session started
    there soon stops at its limit and waits, while another account could work now. `extra`: by
    account id, a planned refill of its long window (`evaluate`)."""
    more = extra or {}
    vs = [evaluate(a, snaps.get(a.id), now, t, more.get(a.id, 0.0)) for a in accounts]
    firm = [evaluate(a, snaps.get(a.id), now, replace(t, grace_hours=0.0), more.get(a.id, 0.0))
            for a in accounts]
    if any(f.eligible for f in firm):
        vs = [f if v.eligible else v for v, f in zip(vs, firm)]
    return sorted(vs, key=lambda v: (-v.score, v.account.id))


def spread(verdicts: list[Verdict], running: dict[str, int], close: float) -> list[Verdict]:
    """Of the eligible accounts scoring within `close` (a fraction) of the best eligible one, the
    one with the fewest sessions `running` (by account id) goes where that best one was; ties keep
    the ranking. Sessions started close together then do not all draw on one account, which the
    score alone cannot see: their usage shows only once they work."""
    eligible = [v for v in verdicts if v.eligible]
    if not eligible:
        return verdicts
    near = [v for v in eligible if v.score >= eligible[0].score * (1 - close)]
    pick = min(near, key=lambda v: running.get(v.account.id, 0))
    out = [v for v in verdicts if v is not pick]
    out.insert(verdicts.index(eligible[0]), pick)
    return out


def prefer(verdicts: list[Verdict], account_id: str) -> list[Verdict]:
    """The user's preferred account first while it is eligible; otherwise the ranking as is."""
    chosen = [v for v in verdicts if v.account.id == account_id and v.eligible]
    return chosen + [v for v in verdicts if v not in chosen]


def versus(first: Verdict, others: list[Verdict]) -> str:
    """Why `first` goes before the next account that can take work, for a reader who sees only
    percentages: the rule and both reasons. Empty when no other account can take work."""
    second = next((v for v in others if v.eligible and v is not first), None)
    if second is None:
        return ""
    a, b = first.account.id, second.account.id
    if first.score >= second.score:
        rule = (f"score {first.score:.2f} vs {second.score:.2f}: room left per hour until it "
                "resets, so room that would expire unused is spent first")
    else:
        rule = (f"score {first.score:.2f} vs {second.score:.2f}, close enough that the one with "
                "fewer sessions running, or the preferred one, goes first")
    return f"{a} before {b} ({rule}); {a}: {first.reason}; {b}: {second.reason}"
