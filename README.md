# fairshare

Use several AI-assistant subscriptions as if they were one. `fairshare` reads each account's
real usage, then starts your next session on the account that makes the best use of what is
left. It aims to use every account fully before its weekly reset and to avoid hitting a short
limit unless that limit is about to reset anyway. It is a heuristic, not a guarantee.

> Status: early. Claude Code on Pro, Max and Ultra plans is the first supported provider.

```
$ fairshare status
> two   claude-pro   score 2.40  seven_day 40% used, resets in 25h
  one   claude-pro   score 0.27  seven_day 70% used, resets in 111h

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

1. `pipx install .` (Python 3.11+). This puts a `fairshare` command on your PATH. fairshare has no
   dependencies when it runs, but installing needs network access once, to fetch the build
   backend (`hatchling`); an offline install fails.
2. `fairshare setup` asks where to keep the hub (default `~/.local/share/fairshare/hub`), lists
   the Claude Code folders it finds (shell aliases that set `CLAUDE_CONFIG_DIR`, and `~/.claude*`
   folders; signed-out ones are marked) and joins each one you pick. Your own folders are only
   ever read: fairshare copies from them and never changes them.
3. `fairshare join [ID] [--from FOLDER]` adds one account: it makes the account's folder in the
   hub, runs Claude Code's own sign-in there (your browser; fairshare never sees a token), and
   checks who signed in. A login that is already another account's is "one quota", and you are
   asked before it is kept. `--from` copies your folder's history, transcripts, settings,
   CLAUDE.md, skills, plugins, commands and agents into the hub (a clashing file keeps both; the
   first folder's settings win and later differences are shown), and, after asking, the
   per-project trust, your MCP servers and the "first run done" flags, never your identity.
4. `fairshare` starts a session on the best account. `fairshare run -p "hi"` (or any other
   arguments) passes them to Claude Code unchanged. `fairshare run -a work -c` starts the account
   `work` instead, skipping the ranking (a warning if it is near a limit); this replaces a
   per-account alias. For a shorter name, make your own alias for `fairshare`.
5. Use each account normally for a while. Every session refreshes that account's snapshot.

**One assistant.** Every account's folder in the hub links to one shared set of settings,
CLAUDE.md, skills, plugins, commands, agents, history, transcripts and project memory, so a
session feels the same whichever account it runs on. Before each launch fairshare checks the
account's links and repairs them without losing anything: a real folder Claude Code made is
merged into the shared one (clashing files keep both), new history lines are appended, a
differing settings file is set aside in `<hub>/backups/` with its diff shown, and it tells you
in one line per item what it changed. fairshare never deletes. Move the hub by moving its folder
and changing `hub` in `accounts.toml`: the links are relative. The usage hook lives in the hub's
settings: `fairshare capture --then '<yours>'` records usage for the account whose folder the
session runs in, then runs your own statusLine and prints its line.

`fairshare leave ID` stops routing to an account; its folder and login stay (`--logout` signs it
out too).

`fairshare run` starts Claude Code as a child process and passes its exit code through; Ctrl-C
and Ctrl-Z reach Claude Code as usual. It starts Claude Code without the variables that
pick a login or a provider (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`), so the account's own login is used, and
prints one line naming any of them it dropped. It also drops the state a surrounding Claude Code
session leaves in its children (silently). Everything else passes through, so model, effort and
your other Claude settings are yours; started from inside a Claude Code session, it names any
other `CLAUDE_*` variable it passes on.

If the sign-in check fails, an account is used only when its login file exists and an earlier
check confirmed it as a subscription login (fairshare keeps a yes/no and a time per account,
nothing else). An account whose plan has no plan file, or whose `accounts.toml` entry is wrong,
is left out with the reason; the others keep working. A bad `accounts.toml` is reported with
the file and line, not a traceback.

`fairshare doctor` checks the setup and changes nothing: the `claude` command is found (with its
version), `accounts.toml` loads, each account's plan exists, each account's sign-in check answers,
the hub's statusLine still calls capture, and the `fairshare` command it calls runs. One line per
check; exit 0 when all pass.

`fairshare uninstall` changes nothing (there is nothing to undo in your own folders): it says
where the config, state and hub are and what the hub holds, how to remove the installed
fairshare (`pipx uninstall fairshare`), and gives you a prompt for your own agent to merge the
hub's history back into your own folders.

Usage comes from Claude Code's documented statusLine input, so fairshare needs no tokens, no
endpoint access and no keychain. A snapshot is only as fresh as that account's last session;
a window past its reset time is treated as unused.

## Adding a provider or plan

- **Plan:** drop a TOML file in `src/fairshare/plans/` (windows and relative capacity; the
  short window is optional). Adding, removing or renaming a subscription changes only plan files;
  an account that still names a missing plan is left out with a message.
- **Provider:** implement the protocol in `providers/base.py` in a new file under
  `src/fairshare/providers/` and register it. Everything the vendor may rename is a named constant
  in that file. The policy never changes.

## Please read

[`docs/terms-and-limits.md`](docs/terms-and-limits.md): what fairshare deliberately does not do,
and what is left for you to decide.

## License

MIT
