# fairshare

Use several AI-assistant subscriptions as if they were one. `fairshare` reads each account's
real usage, then starts your next session on the account that makes the best use of what is
left. It aims to use every account fully before its weekly reset and to avoid hitting a short
limit unless that limit is about to reset anyway. It is a heuristic, not a guarantee.

> Status: early. Claude Code on Pro, Max and Ultra plans is the first supported provider.

```
$ fairshare status
> two   claude-pro   score 2.40  week 40% used, resets in 25h
  one   claude-pro   score 0.27  week 70% used, resets in 111h

$ fairshare       # starts a session on the account at the top of that list
```

## How it decides

For each account, `score = capacity × (100 − weekly used %) ÷ hours until the weekly reset`:
the budget it must burn per hour to avoid wasting any. The highest score wins, which sends
work to the account whose reset is nearest and whose budget is least used.

- If the short (5-hour) window is nearly full and not resetting within the hour, the score is
  scaled by what is left of that window. Such an account can still win when its weekly budget
  is far larger than the others'.
- An account whose week is spent ranks below every usable account, and among spent accounts
  the one that resets soonest ranks first.

The rule is one pure function in [`src/fairshare/policy.py`](src/fairshare/policy.py).
Known limit: below the "nearly full" line, short-window usage plays no part in the ranking.

## Setup

1. `pipx install .` (Python 3.11+, no dependencies). This puts a `fairshare` command on your PATH.
2. `fairshare init` lists the accounts it finds (shell aliases that set `CLAUDE_CONFIG_DIR`,
   and signed-in `~/.claude*` directories), asks you to confirm each id and plan, shows the
   resulting `accounts.toml` as a diff and writes it only on yes, keeping a backup. It proposes the
   plan Claude Code reports for an account when it can tell, and never opens a login file.
   Non-interactive: `--yes`, or `--account id=dir:plan` (repeatable) to name the accounts yourself.
3. `fairshare apply --dry-run` prints every change as a diff; `fairshare apply` makes them,
   asking per file and backing each up first. It adds a capture `statusLine` to each account's
   settings.json, calling the installed `fairshare` by its full path. One you already have is
   wrapped, not replaced: `fairshare capture <id> --then '<yours>'` records usage, then runs
   yours on the same input and prints its line. It touches nothing else: no PATH entry, alias
   or shell rc line.
4. `fairshare` starts a session on the best account. `fairshare run -p "hi"` (or any other
   arguments) passes them to Claude Code unchanged. `fairshare run -a work -c` starts the account
   `work` instead, skipping the ranking (a warning if it is near a limit); this replaces a
   per-account alias. For a shorter name, make your own alias for `fairshare`.
5. Use each account normally for a while. Every session refreshes that account's snapshot.

`fairshare run` starts Claude Code as a child process and passes its exit code through; Ctrl-C
and Ctrl-Z reach Claude Code as usual. It starts Claude Code without `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`, so the account's own login is used, and
prints one line naming any of them it dropped. Model and effort are yours: your Claude defaults
apply.

`fairshare doctor` checks the setup and changes nothing: the `claude` command is found (with its
version), each account's sign-in check answers, the `fairshare` command a statusLine calls runs,
and each account's statusLine still calls capture. One line per check; exit 0 when all pass.

`fairshare uninstall` reverses step 3 the same way (diff, ask, back up): it unwraps each
statusLine, and also removes a plain fairshare capture `statusLine` you added by hand.
`accounts.toml` and snapshots stay.

Usage comes from Claude Code's documented statusLine input, so fairshare needs no tokens, no
endpoint access and no keychain. A snapshot is only as fresh as that account's last session;
a window past its reset time is treated as unused.

## Adding a provider or plan

- **Plan:** drop a TOML file in `src/fairshare/plans/` (windows and relative capacity; the
  short window is optional). Adding, removing or renaming a subscription changes only plan files.
- **Provider:** implement the protocol in `providers/base.py` in a new file under
  `src/fairshare/providers/` and register it. Everything the vendor may rename is a named constant
  in that file. The policy never changes.

## Please read

[`docs/terms-and-limits.md`](docs/terms-and-limits.md): what fairshare deliberately does not do,
and what is left for you to decide.

## License

MIT
