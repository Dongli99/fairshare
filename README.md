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
   folders Claude Code has run in; each marked by Claude Code's own sign-in check when it is
   signed out or unknown) and joins each one you pick. Your own folders are only ever read:
   fairshare copies from them and never changes them.
3. `fairshare join [ID] [--from FOLDER]` adds one account: it makes the account's folder in the
   hub, runs Claude Code's own sign-in there (your browser; fairshare never sees a token), and
   checks who signed in. A login that is already another account's is "one quota", and you are
   asked before it is kept. On a terminal it first asks which browser that account's sign-in
   links open in (below). `--from` copies your folder's history, transcripts, settings,
   CLAUDE.md, skills, plugins, commands, agents, rules and workflows into the hub (a clashing
   file keeps both; the first folder's settings win and later differences are shown), and,
   after asking, the per-project trust, your MCP servers, the "first run done" flags and the
   Claude in Chrome consent, never your identity. `fairshare join ID` for an account already
   joined signs it in again and changes nothing else: the fix for an expired or lost login.
4. `fairshare` starts a session on the best account. `fairshare run -p "hi"` (or any other
   arguments) passes them to Claude Code unchanged. `fairshare run -a work -c` starts the account
   `work` instead, skipping the ranking (a warning if it is near a limit); this replaces a
   per-account alias. For a shorter name, make your own alias for `fairshare`.
5. Use each account normally. Every launch asks each account for its usage (below), and every
   session refreshes its account's snapshot as it goes.

**Usage at launch.** `fairshare`, `fairshare run` and `fairshare status` ask Claude Code for each
account's usage before ranking: one `claude -p "/usage"` per account, all at once, about a
second in total. It is Claude Code's own local `/usage` command, so no model call is made and
nothing is spent; your own hooks are switched off for it (`--settings
'{"disableAllHooks":true}'`), so they do not run once per account at every launch. Usage spent
anywhere (the web app, the desktop app, a session started outside fairshare) is therefore seen.
What it means when you see:
- `usage probe failed for <id> (<reason>); using its snapshot from 14:05`: that account could not
  be asked (a timeout, signed out, an answer fairshare did not understand), so it is ranked on
  what its last session recorded (or as a fresh window, with no snapshot). The launch goes on.
- `(usage as of 14:05)` on a `status` row: those numbers are from a snapshot taken then, not
  from this launch.
- `warning: the usage probe for <id> ran as a model turn ...`: a Claude Code release stopped
  treating `/usage` as a local command, so asking spends usage. fairshare does not use the
  answer and says so every time until you turn the probe off.
- `warning: <id>'s Fable week is spent until Thu 03:00; other models are unaffected`: a
  per-model weekly limit is used up on the account that is starting. The ranking does not
  change: which model you use is up to you.
- `<id>: sign-in ends Fri 10:50; \`fairshare join <id>\` signs it in again`: that account's
  sign-in ends within three days.

To turn the probe off for an account, add `usage_args = []` to its table in `accounts.toml`; it
then ranks on its snapshot alone. `usage_args` can also replace the command (a string, split like
a shell would, or a list), as `status_args` does for the sign-in check; without
`--output-format json` fairshare can no longer tell whether the answer came from the model.

**When an account runs out.** Several sessions on one account share its limit, and the last
few percent can go in minutes, so fairshare does not try to hand work over before the limit.
When Claude Code stops a turn because the account hit its limit (its `StopFailure` hook, which
fairshare wires in the hub's settings), the session is marked as stopped, and:
- in a session started by `fairshare`, the launcher checks that the account really is out (it
  asks every account's usage again; a passing rate-limit error changes nothing), stops the old
  session and shows `work hit its limit (5h resets 15:40). Continuing on play in 10 s · Enter =
  now · c = cancel`. A new session then starts on the best account, in the same folder, with a
  first prompt telling it which session it continues and how to read where that one stopped.
  `c` stops there and prints how to resume the old session later (`fairshare run -a work --
  --resume <id>`). Without a terminal it continues at once. The launcher stays in charge, so the
  next limit is picked up the same way;
- the status bar ends in a red `out · ! fairshare pickup` while that account cannot take work.
  Typing `! fairshare pickup` in the session asks its launcher to move it now (no check that the
  account is out: you asked); in a session not started by `fairshare`, it starts the
  continuation in the background instead (`claude --bg`, on the best account that already
  trusts the folder; Claude Code prints how to attach to it) and leaves your session alone;
- from a terminal, `fairshare pickup <id>` (the session id or its first characters) continues a
  recorded session in the foreground, in its folder; `--bg` does it in the background.

fairshare never stops a session it did not start. `fairshare transcript <id> [--tail N]` prints
what a recorded session said (the last 40 messages by default), words only: no tool calls,
tool output or thinking.

**Fresher usage in a session.** A snapshot's time now says when its numbers were last known to
be current: a status bar that redraws the same numbers no longer makes them look fresh. When
an account's numbers are older than `refresh_minutes` (default 5, at least 1; set it under
`[policy]` in `accounts.toml`), the status bar asks Claude Code's `/usage` for them again in the
background (one at a time per account, never slowing the bar) and shows `as of 14:05` until
they arrive. The bar also redraws once a minute (`refreshInterval`, added to the hub's
statusLine when it has none).

**One assistant.** Every account's folder in the hub links to one shared set of settings,
CLAUDE.md, skills, plugins, commands, agents, history, transcripts and project memory, so a
session feels the same whichever account it runs on. Before each launch fairshare checks the
account's links and repairs them without losing anything: a real folder Claude Code made is
merged into the shared one (clashing files keep both), new history lines are appended, a
differing settings file is set aside in `<hub>/backups/` with its diff shown, and it tells you
in one line per item what it changed. fairshare never deletes.

**Moving the hub.** Move its folder, then run `fairshare doctor --fix` (it asks where the hub is
now, or takes `--hub PATH`): it changes `hub` in `accounts.toml`, relinks every account (the
links are relative, so they hold), rewires the usage hook if needed, and shows you every place
in the shared settings that names the old path (your own hooks, say), renaming them only after
your yes. Until then a launch stops with "no hub at <path>" instead of quietly building a new,
empty hub at the old path. On macOS the login lives in the keychain, one entry per config folder,
so after a move accounts may read as signed out: `doctor --fix` names `fairshare join ID` for each.
The usage hook lives in the hub's
settings: `fairshare capture --then '<yours>'` records usage for the account whose folder the
session runs in, then runs your own statusLine and prints its line.

**Folder trust.** Claude Code asks whether you trust a folder, and remembers the answer per
account, so a folder you trusted on one account asks again on the next. When you start a session
on a terminal in a folder that some account does not trust yet, fairshare asks once, before
Claude Code starts, with Claude Code's own warning (it will be able to read, edit and execute
files there):
- `[a]` trust it for all accounts that lack it (named);
- `[o]` only the account about to start, which fairshare remembers for this folder: it asks
  again only when a launch lands on an account that still lacks trust there;
- `[n]` no: nothing is written and nothing starts.

There is no default (Enter asks again). When the account about to start already trusts the
folder, `[n]` is not offered and `[o]` leaves the others as they are. `fairshare run -a ID` asks
about that account only. In your home folder itself fairshare never asks or writes (Claude Code
keeps trust there for one session only, by design), and without a terminal it asks nothing and
Claude Code asks for itself. What it writes is exactly what Claude Code's own "Yes, I trust this
folder" writes, in the same place, with a backup in `<hub>/backups/`.

**Browser per account.** If you keep each account in its own browser profile, join (and setup)
asks which one opens that account's sign-in: the default browser, one of your Chrome profiles
(listed by name), or a command. It is stored as one line in the account's table in
`accounts.toml` (`browser = "chrome:Profile 1"` or `browser = "command:/path/to/program"`); edit
or remove that line to change it. fairshare writes a one-line launcher for it in
`<hub>/browsers/` and points `BROWSER` at it for that account's sign-in and sessions. Whether
`/login` inside a session honours `BROWSER` is not verified.

`fairshare prefer ID [--for 3h]` prefers one account for a while (default: the rest of today):
launches go to it while it can take work, and to the best account when it cannot. It ends on
its own; `fairshare prefer --off` ends it now, `fairshare prefer` shows it.

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

If the sign-in check fails, an account is used only when an earlier check confirmed it as a
subscription login (fairshare keeps a yes/no and a time per account, nothing else). A signed-out
account is skipped with a hint (`fairshare join ID` signs it in again); `fairshare run -a ID` on
a signed-out account offers that sign-in on a terminal and then starts the session. An account whose plan has no plan file, or whose `accounts.toml` entry is wrong,
is left out with the reason; the others keep working. A bad `accounts.toml` is reported with
the file and line, not a traceback.

`fairshare doctor` checks the setup and changes nothing: the `claude` command is found (with its
version), `accounts.toml` loads, the hub is where it is configured, each account's plan exists,
its sign-in check answers (with the date the sign-in ends), its usage probe answers (or is off)
and its `.claude.json` still holds folder trust where fairshare expects it, the hub's statusLine still calls capture, its stop-failure hook still calls `fairshare hook`, and the `fairshare` command they call runs.
One line per check; exit 0 when all pass. `fairshare doctor --fix` repairs what it can first
(a moved hub, missing links, a stale or missing hook), one line per fix.

`fairshare uninstall` changes nothing (there is nothing to undo in your own folders): it says
where the config, state and hub are and what the hub holds, how to remove the installed
fairshare (`pipx uninstall fairshare`), and gives you a prompt for your own agent to merge the
hub's history back into your own folders.

Usage comes from Claude Code's own `/usage` command at launch and its documented statusLine
input during a session, so fairshare needs no tokens, no endpoint access and no keychain. A
window past its reset time is treated as unused. A session's status bar also shows a per-model
weekly limit once it is nearly used up (`Fable 96% (Thu 03:00)`).

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
