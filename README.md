# fairshare

Use several AI-assistant subscriptions as if they were one. `fairshare` reads each account's
real usage, then starts your next session on the account that makes the best use of what is
left: every account used fully before its weekly reset, and none hitting its short limit
unless that limit is about to reset anyway.

> Status: early. Claude Code on Pro and Max plans is the first supported provider.

```
$ fairshare status
> two   claude-pro   score 2.40  week 40% used, resets in 25h
  one   claude-pro   score 0.27  week 70% used, resets in 111h

$ alias claude='fairshare run --'      # then just type: claude
```

## How it decides

For each account, `score = capacity × (100 − weekly used %) ÷ hours until the weekly reset`:
the budget it must burn per hour to avoid wasting any. The highest score wins, which sends
work to the account whose reset is nearest and whose budget is least used. An account whose
short (5-hour) window is nearly full is skipped unless it resets within the hour. If every
account is near a limit, the one that frees up first is chosen. The rule is one pure function
in [`src/fairshare/policy.py`](src/fairshare/policy.py).

## Setup

1. `pipx install .` (Python 3.11+, no dependencies).
2. `fairshare init` prints a sample `accounts.toml` and the one-line `statusLine` entry to add
   to each account's settings. fairshare never edits your settings.
3. Use each account normally for a while. Every session refreshes that account's snapshot.

Usage comes from Claude Code's documented statusLine input, so fairshare needs no tokens, no
endpoint access and no keychain. A snapshot is only as fresh as that account's last session;
a window past its reset time is treated as unused.

## Adding a provider or plan

- **Plan:** drop a TOML file in `src/fairshare/plans/` (windows and relative capacity).
- **Provider:** implement `parse_capture` and `launch` in `src/fairshare/providers/`
  (see `providers/base.py`) and register it. The policy never changes.

## Please read

[`docs/terms-and-limits.md`](docs/terms-and-limits.md): what fairshare deliberately does not do,
and what is left for you to decide.

## License

MIT
