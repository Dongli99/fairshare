"""What a percent of a plan window costs in dollars, learned from the account's usage log (pure).

The log (`store.usage_log`) holds, per changed snapshot, the dollars the account's sessions drew
since the line before and each window's used % and reset. Within one window instance (one
reset time) usage only rises, so the dollars between two lines of the same instance bought the
rise between them. The fit pools those pairs over recent instances. Read-only: shown by
`status`, decided on by nothing yet."""
from __future__ import annotations

from dataclasses import dataclass

from .model import Window

SAME_RESET_S = 120          # consecutive resets this close are one instance (given to the minute)
KEEP_S = 14 * 24 * 3600     # log lines older than this are left out (plans and model mix change)
MIN_RISE_PCT = 3.0          # an instance that rose less is too coarse for whole-percent numbers
MIN_SHORT_INSTANCES = 2     # the short window needs this many qualifying instances for a rate
MIN_LONG_INSTANCES = 1      # the long window needs this many (one week is already many points)
MIN_TOTAL_RISE_PCT = 10.0   # fewer pooled points than this give no rate, whatever the count

Line = tuple[float, "float | None", dict[str, tuple[float, float]]]   # (t, spent, windows)


@dataclass(frozen=True)
class Rate:
    per_pct: float       # dollars per percentage point, pooled over the instances
    instances: int       # window instances that counted
    low: float           # the lowest and highest per-instance rate: how well the fit holds
    high: float


def fit(lines: list[Line], key: str, now: float, min_instances: int) -> Rate | None:
    """The dollars per point of window `key` from the log `lines` (in log order) of the last
    KEEP_S, or None when there is too little data. A pair of consecutive lines counts only when
    both carry the window with the same reset: a reset in between leaves its dollars ambiguous,
    and the first line has no dollars. A step down (the provider credited usage back) counts as
    no rise, not as a negative one."""
    spent: dict[float, float] = {}     # instance (its first reset) -> dollars, points
    rise: dict[float, float] = {}
    inst, last, prev = None, None, None   # current instance, its latest reset, previous line's %
    for t, dollars, windows in lines:
        w = windows.get(key) if t >= now - KEEP_S else None
        if w is None:
            prev = None
            continue
        used, resets_at = w
        if last is None or abs(resets_at - last) > SAME_RESET_S:
            inst, prev = resets_at, None
        if prev is not None and dollars is not None:
            spent[inst] = spent.get(inst, 0.0) + dollars
            rise[inst] = rise.get(inst, 0.0) + max(0.0, used - prev)
        last, prev = resets_at, used
    rates = [(spent[i], rise[i]) for i in rise if rise[i] >= MIN_RISE_PCT]
    total = sum(r for _, r in rates)
    if len(rates) < min_instances or total < MIN_TOTAL_RISE_PCT:
        return None
    each = [d / r for d, r in rates]
    return Rate(sum(d for d, _ in rates) / total, len(rates), min(each), max(each))


def fit_plan(lines: list[Line], short: str | None, long: str | None, now: float
             ) -> dict[str, Rate]:
    """The rate of each of the plan's windows that has one."""
    found = {k: fit(lines, k, now, n) for k, n in
             ((short, MIN_SHORT_INSTANCES), (long, MIN_LONG_INSTANCES)) if k}
    return {k: r for k, r in found.items() if r}


def room(rates: dict[str, Rate], windows: dict[str, Window], warn_pct: float, now: float
         ) -> dict[str, float]:
    """Dollars left before `warn_pct` in each window that has a rate. A window already reset,
    or absent from the snapshot (gone at its reset, nothing seen since), counts as unused."""
    out = {}
    for k, r in rates.items():
        w = windows.get(k)
        used = w.used_pct if w and w.resets_at > now else 0.0
        out[k] = max(0.0, warn_pct - used) * r.per_pct
    return out
