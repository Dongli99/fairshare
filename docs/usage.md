# Using fairshare

Every command, message and setting. For what fairshare is and how to install it, see the
[README](../README.md); for why it works this way, [architecture.md](architecture.md).

## Words

- **Hub**: the folder fairshare keeps (default `~/.local/share/fairshare/hub`). It holds one
  Claude Code config folder per account, all linked to one shared set of settings, history and
  skills.
- **Main session**: a session started by `fairshare`. It hears fairshare's warnings and decides
  for itself and its workers.
- **Worker**: a background Claude Code session a main session starts for big or long work.
- **Pickup**: the same session continues on another account, with its whole context.
- **Handoff**: a fresh session starts from a note the old one wrote.
- **Switch**: a fresh session starts with nothing carried over but the folder, model and effort.
- **Checkpoint**: a dated note a session writes about where it stands.
- **Launcher**: the `fairshare` process that started a session and stays its parent.

## Setup and accounts

`fairshare setup` asks where to keep the hub (default `~/.local/share/fairshare/hub`), lists the
Claude Code folders it finds (shell aliases that set `CLAUDE_CONFIG_DIR`, and `~/.claude*`
folders Claude Code has run in), and joins each one you pick. Each folder is marked when Claude
Code's own sign-in check says it is signed out or not a subscription login, or when it could not
be checked (`sign-in unknown`). fairshare never writes your folders; to label them it runs Claude
Code's own sign-in check there. A folder with no `.claude.json` is not checked, since the check
would create one.

The prompt says that the first folder joined decides the shared settings, CLAUDE.md and keybindings, and
shows the order `all` would use: signed-in folders first, and among those the default
`~/.claude` first; then the rest. Typing numbers joins in the order you type. On a terminal
each folder joined is first asked a name (`Name for ~/.claude [claude]:`; Enter keeps the
folder's alias or name, and a name that is not a valid id or is already an account's or a hub
folder's is asked again); without a terminal the alias or name is used. When a later
folder's settings differ, the diff is shown, followed by one line naming the file to edit if you
want that folder's settings instead. A file that is new in the hub (fairshare's skill on a first
setup, say) is reported in one line (`installed <what> in <path>`); diffs are kept for changes to
a file that already exists.

Then, while the shared settings say nothing about it, setup asks once whether to turn on Claude
Code's Remote Control for every session (follow and steer them from claude.ai/code or the Claude
app). Enter turns it on, for every account at once, since they share one settings file; `n`
leaves the settings alone. A setting already there, from the folder that seeded them, is kept.

Last, when an alias of yours still starts Claude Code on a folder you just joined, setup offers
to take it over (see [Your own aliases](#your-own-aliases)).

setup always leaves a complete hub, even when it joins nothing; until an account is joined,
`fairshare`, `run` and `status` say ``no accounts yet: `fairshare join <name>` signs one in``
(exit 1). If the `claude` command is not on PATH, setup still writes the config and builds the
hub, then says once that Claude Code must be installed (``no Claude Code command found on
PATH; install it, then run `fairshare setup` again to join accounts``) and joins nothing (exit 1). It
ends with ``start sessions with `fairshare`; plain `claude` still uses ~/.claude, outside the
hub``. fairshare's only edit to a shell file is its block of aliases, shown and asked first.

`fairshare join [ID] [--from FOLDER]` adds one account. It makes the account's folder in the
hub, runs Claude Code's own sign-in there (your browser; fairshare never sees a token), and
checks who signed in. If the `claude` command is not on PATH it says so and stops before
creating any folder. Without an ID, a terminal is asked a name (`Name for the new account
[account-1]:`, or the `--from` folder's alias or name as the default); without a terminal the
default is used. On a terminal it then asks which browser that account's sign-in links open in
(see [Browser per account](#browser-per-account)).

A login that is already another account's shares its quota, so keeping both adds nothing. On a
terminal join says so (`play signed in as the same account as work, so they share one quota:
keeping both adds nothing.`), explains that the browser was usually still signed in to that
account (sign out on claude.ai, or choose another browser profile), and offers `[Enter] sign in
again · k = keep anyway · n = don't add`. Enter runs the sign-in in the same folder again,
offering the browser choice again, and checks again; `k` keeps it; `n` leaves it out (its
folder stays). Without a terminal it is not added, and join says `same login as work: one
quota`. `--from` copies your folder's
history, transcripts, settings, CLAUDE.md, skills, plugins, commands, agents, rules and
workflows into the hub (a clashing file keeps both; the first folder's settings win and later
differences are shown), and, after asking, the per-project trust, your MCP servers, the "first
run done" flags and the Claude in Chrome consent. Never your identity, and never `sessions/`
(each running session's key, stale on arrival). The source folder is never written.

`--from` also records the folder in the account's table (`from = "..."`), so that an alias
naming that folder can be taken over even when the account got another name. When the joined
folder has an alias, or fairshare's block of aliases exists, join offers the new account's line
in the block, with its own yes (see [Your own aliases](#your-own-aliases)).

`fairshare join ID` for an account already joined signs it in again and changes nothing else:
the fix for an expired or lost login.

### Your own aliases

Joining copies a folder into the hub and leaves the folder as it was, so an alias such as
`alias work='CLAUDE_CONFIG_DIR=~/.claude-work claude'` would go on starting the old folder:
its sessions would not be in the hub, and `work agents --json` or `work daemon status` would
answer about the old folder, wrongly and without a word. `fairshare aliases` takes such aliases
over with one block at the end of the shell file where it found them:

```sh
# >>> fairshare aliases >>>
alias work='fairshare run -a work'
# <<< fairshare aliases <<<
```

Being last, the block overrides your own line above it, which stays as it is. It shows the diff
(fairshare's lines only), says which of your aliases it overrides, and asks `Change ~/.zshrc?
[Y/n]` (Enter: yes); the file is backed up beside it first (`.zshrc.bak-fairshare-<time>`). Run
it again and it changes nothing; `fairshare aliases --remove` takes the block out, the same way,
and gives you back the file as it was.

- Which aliases: those that set `CLAUDE_CONFIG_DIR` to a folder joined into the hub, matched by
  the folder (the `from` that join records) whatever the account is named; an account joined
  before join recorded it is matched by name instead. Aliases to other folders, and your other
  aliases, are left alone.
- An alias that passes more than the folder (`... claude --model opus`, or other variables) is
  left alone, with one line giving the alias you could write yourself
  (`alias wm='fairshare run -a work --model opus'`).
- Without a terminal nothing is written: the block is printed with how to apply it. A shell file
  that is a link (a dotfile manager such as stow, chezmoi or home-manager) or cannot be written
  is never written either: paste the printed block into your own source file.
- The shells read: `~/.zshrc`, `~/.zprofile`, `~/.zshenv`, `~/.bashrc`, `~/.bash_profile`,
  `~/.bash_aliases`, `~/.profile`. Only `alias NAME=...` lines are seen, not shell functions.

Through the alias, `work` starts a session on the account `work` (as `fairshare run -a work`),
and Claude Code's own subcommands still work: `work agents --json`, `work rm <id>`,
`work mcp list`, `work daemon status` go straight to Claude Code on that account's hub folder,
with nothing of fairshare around them (see [Starting a session](#starting-a-session)).

`fairshare leave ID` stops routing to an account; its folder and login stay (`--logout` signs it
out too).

`fairshare prefer ID [--for 3h]` prefers one account for a while (default: the rest of today):
launches go to it while it can take work, and to the best account when it cannot. It ends on its
own; `fairshare prefer --off` ends it now, `fairshare prefer` shows it.

## Starting a session

`fairshare` starts a session on the best account. `fairshare run -p "hi"` (or any other
arguments) passes them to Claude Code unchanged. `fairshare run -a work -c` starts the account
`work` instead, skipping the ranking (with a warning if it is near a limit); this replaces a
per-account alias. Only a leading `-a` is fairshare's: `fairshare run -- -a x` passes `-a x` on.
For a shorter name, make your own alias for `fairshare`.

When the first argument is one of Claude Code's own subcommands (`agents`, `attach`, `auth`,
`auto-mode`, `daemon`, `doctor`, `gateway`, `import`, `install`, `logs`, `mcp`, `plugin`,
`purge`, `respawn`, `rm`, `setup-token`, `stop`/`kill`, `ultrareview`, `update`), no session
starts: `fairshare run -a work agents --json` runs `claude agents --json` on `work`'s hub folder
and nothing else (no sign-in check, ranking, warning or record), with Claude Code's own output
and exit code. Without `-a` it is refused (exit 2), since such a command is about one account:
``fairshare: `claude agents` is about one account's folder; name the account: fairshare run -a
ID agents``. `fairshare agents` says the same.

When two accounts are within 10% of each other, a new session goes to the one with fewer
sessions running, so sessions started together spread out. `fairshare status` marks with `>` the
account the next session would start on, and its last line says why that account goes before the
next one: both scores, the rule, and both accounts' reasons (`why a: a before b (score 1.18 vs
0.42: ...)`). Percentages alone can point the other way: room that resets sooner is spent first,
and a planned full reset counts as room (its row says how much is left until the reset is used).
The warning's best other account and `fairshare worker` say the same.

If the `claude` command is not on PATH, `fairshare`, `run`, `pickup`, `handoff` and `join` say
so first and stop, before any sign-in check or usage probe.

**The launch pause.** When the choice is close (the chosen account is nearly spent, the next one
is within 10% of it, or a warning was printed), `fairshare` pauses for a moment on a terminal:
it lists the accounts that can take work and shows `starting a in 3 s · Enter = now · 1-2 = that
account · q = quit`. Set `launch_pause = 0` under `[policy]` in `accounts.toml` to turn it off
(or another number of seconds). `run -a`, pickups and handoffs never pause; a switch does.

**When no account can take work.** fairshare prints one line per account (why, and when it is
usable again). On a terminal it offers `[w]ait` for the soonest account, `[s]tart` on the top
one anyway, or `[q]uit` (the default). Waiting checks every account again at that time and
re-ranks. Without a terminal it exits 75.

**Signed-out accounts.** If the sign-in check fails, an account is used only when an earlier
check confirmed it as a subscription login (fairshare keeps a yes/no and a time per account,
nothing else). A signed-out account is skipped with a hint (`fairshare join ID` signs it in
again); `fairshare run -a ID` on a signed-out account offers that sign-in on a terminal and then
starts the session. An account whose plan has no plan file, or whose `accounts.toml` entry is
wrong, is left out with the reason; the others keep working. A bad `accounts.toml` is reported
with the file and line, not a traceback.

**The environment.** `fairshare run` starts Claude Code as a child process and passes its exit
code through; Ctrl-C and Ctrl-Z reach Claude Code as usual. It starts Claude Code without the
variables that pick a login or a provider (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`), so the account's own login is used, and
prints one line naming any of them it dropped. It also drops, silently, the state a surrounding
Claude Code session leaves in its children. Everything else passes through, so model, effort and
your other Claude settings are yours. Started from inside a Claude Code session, it names any
other `CLAUDE_*` variable it passes on. Variables set in the shared settings' `env` are not
looked at (see [Known limits](architecture.md#known-limits)).

## Usage at launch

`fairshare`, `fairshare run` and `fairshare status` ask Claude Code for each account's usage
before ranking: one `claude -p "/usage"` per account, all at once, about a second in total. It is
Claude Code's own local `/usage` command, so no model call is made and nothing is spent. Your own
hooks are switched off for it (`--settings '{"disableAllHooks":true}'`), so they do not run once
per account at every launch. Usage spent anywhere (the web app, the desktop app, a session
started outside fairshare) is therefore seen. A window past its reset time is treated as unused.

What the messages mean:
- `usage probe failed for <id> (<reason>); using its snapshot from 14:05`: that account could not
  be asked (a timeout, signed out, an answer fairshare did not understand), so it is ranked on
  what its last session recorded (or as a fresh window, with no snapshot). The launch goes on.
- `(usage as of 14:05)` on a `status` row: those numbers are from a snapshot taken then, not from
  this launch.
- `warning: the usage probe for <id> ran as a model turn ...`: a Claude Code release stopped
  treating `/usage` as a local command, so asking spends usage. fairshare does not use the answer
  and says so every time until you turn the probe off.
- `warning: <id>'s Fable week is spent until Thu 03:00; other models are unaffected`: a per-model
  weekly limit is used up on the account that is starting. The ranking does not change: which
  model you use is up to you.
- `<id>: sign-in ends Fri 10:50; \`fairshare join <id>\` signs it in again`: that account's
  sign-in ends within three days.

To turn the probe off for an account, add `usage_args = []` to its table in `accounts.toml`; it
then ranks on its snapshot alone. See [accounts.toml](#accountstoml) for replacing the command.

**Fresher usage in a session.** A snapshot's time says when its numbers were last known to be
current: a status bar that redraws the same numbers does not make them look fresh. When an
account's numbers are older than `refresh_minutes` (default 5), the status bar asks Claude Code's
`/usage` for them again in the background (one at a time per account, never slowing the bar) and
shows `as of 14:05` until they arrive. The bar redraws once a minute (`refreshInterval`, added to
the hub's statusLine when it has none). It also shows a per-model weekly limit once it is nearly
used up (`Fable 96% (Thu 03:00)`).

## A full reset this week

claude.ai sometimes grants an account a full reset, with a date to use it by. Using it
(`/limit-reset` in a session on that account, or claude.ai's usage page) refills the 5-hour and
the weekly limit now; the weekly reset day stays the same. Claude Code does not tell fairshare
which resets an account holds, so you tell it: pick a busy week and plan the reset.

`fairshare full-reset ID` plans it for this week: the account then ranks as if its week had 100%
more room (`status` shows `+100% planned` on its row and when the plan ends). A spent week still
takes no work until the reset is used. `fairshare full-reset` shows the plans,
`fairshare full-reset ID --cancel` drops one (`--cancel` alone drops them all); planning again
replaces it. An account whose current week fairshare has not seen yet cannot be planned
(`fairshare status` asks for its usage).

The status bar marks the week: `7d 20% +reset`, and `7d 92% (Sun 10:00) use reset` once the week
is at 90% or more. From then on a warning on that account (to a main session or a worker) says
not to move: keep working there until the limit stops the session, then you use the reset and the
stopped sessions continue on the same account. The refill is worth most on a spent week; moving
at 90% would leave the last tenth unused (on Pro, about a whole 5-hour window). A 5-hour
crossing while the week is below 90% warns as usual and keeps the reset for a fuller week.

The plan ends when fairshare sees the reset used: a usage probe that finds the 5-hour or the
weekly window lower than before under the same reset (the status bar's own lower numbers are
taken for a stale redraw, so the next probe, within `refresh_minutes`, is what notices), or a
window's reset time moving. It also ends at the regular weekly reset, used or not: a plan never
carries into the next week. Usage the provider credits back for another reason ends it too,
early; the account then simply ranks on its real numbers.

## When an account runs out

Several sessions on one account share its limit, and the last few percent can go in minutes, so
fairshare does not try to hand work over before the limit. When Claude Code stops a turn because
the account hit its limit (its `StopFailure` hook, which fairshare wires in the hub's settings),
the session is marked as stopped, and:

- **In a session started by `fairshare`**, the launcher checks that the account really is out (it
  asks every account's usage again; a passing rate-limit error changes nothing). Then, by default,
  it leaves the session alone: Claude Code resumes it by itself at the reset, with its whole
  context and no re-read. One line says so once, `fairshare: work is out (5h limit) · Claude Code
  resumes this session at 15:40 · ! fairshare pickup moves it to play now` (or `no account can
  take work now (the soonest: play, usable at 16:10)`), and the status bar reads `out · 5h limit ·
  resumes 15:40 · ! fairshare pickup moves it to play now` until the reset.
- **To move automatically instead**, set `resume_wait_minutes` under `[policy]` in
  `accounts.toml`: the launcher then moves the session when the reset is more than that many
  minutes away (`0` = always), and only when another account can take work; otherwise it waits as
  above. A move stops the session and shows `work hit its limit (5h resets 15:40). Continuing on
  play in 10 s · Enter = now · c = cancel`. The same session is then resumed on the best account,
  in the same folder, with its model and effort, and is told in one line that it was moved. `c`
  stops there and prints how to resume it later (`fairshare run -a work -- --resume <id>`).
  Without a terminal it continues at once. The launcher stays in charge, so the next limit is
  handled the same way.
- **Never back onto an account that is out.** An account whose week or short window is spent
  is never chosen for a launch, pickup, switch or handoff until it resets. Independently of the
  ranking, the launcher remembers which account's limit stopped a session and when it resets,
  and never resumes the session there before then (`fairshare: work's limit stopped this session
  and lifts at 15:40; not resuming it there before then. To resume it later: …`). If the same
  limit stops a session again after a move, the launcher stops instead of trying again, with
  `fairshare: work stopped this session again before its limit lifted (5h resets 15:40); not
  trying again. To resume it later: fairshare run -a work -- --resume <id>`.
- **The status bar** ends in a red `out · ! fairshare pickup` while that account cannot take work.
  Typing `! fairshare pickup` in the session asks its launcher to move it now (no check that the
  account is out, no wait for the reset: you asked), to the best account that can take work. When
  none can, it says `fairshare: no account can take work now; the soonest is play, usable at
  16:10; nothing done` and leaves the session as it is. In a session not started by `fairshare`, it
  resumes the session in the background instead (`claude --bg --resume`, on the best account that
  trusts the folder or can get that trust copied from another account; Claude Code prints how to
  attach to it; background sessions open links in the system browser).
- **From a terminal**, `fairshare pickup <id>` (the session id or its first characters) resumes a
  recorded session in the foreground, in its folder; `--bg` does it in the background. Either
  way, a session that was not stopped by its limit (it may still be running, and fairshare never
  stops a session it did not start) is continued as a copy under a new id (`--fork-session`), so
  two processes never write one transcript. The original stays as it was, with its account, its
  workers and its line in `fairshare status`.

Moving a session to another account reads its whole context once on that account, uncached: that
is the price of keeping its reasoning. For a lean start use a handoff. Each account is its own
organization to Claude Code, so a session resumed on another account goes on under a new id
(fairshare links it to the old one: role, workers, checkpoint note) and Claude Code drops its
earlier thinking with a notice. Resumed on the same account it keeps its id and loses nothing.

## Pickup, handoff, switch

Three ways to continue in another session, each with `--model M`, `--effort E` (override the
session's latest, which carry over otherwise) and `-a ID` (that account, skipping the ranking;
the folder trust check still applies):

| command | what starts |
|---|---|
| `fairshare pickup` | the same session, resumed on the best account, with its whole context |
| `fairshare handoff ["<note>"]` | a fresh session that first reads a handoff note: what was done, what is next, open decisions, files touched |
| `fairshare` (bare, inside a session) | a fresh session with nothing carried but the folder, the model and the effort (a switch) |

**Handoff with a note** (what the agent runs) saves the note as the session's checkpoint. When
your project already has its own way to hand off (a handoff file it writes somewhere, say), the
skill tells the agent to follow it and pass that file's path as the note, so the fresh session
reads your file; fairshare only carries the pointer. In a
session started by `fairshare`, the launcher then stops the session and starts the fresh one.
Without a launcher, the fresh one starts in the background: fairshare prints `handing off <id> to
a fresh session on <account>, in the background`, the fresh session is told that the old one
handed over at that time and may still be running, and the command ends with `this session keeps
running: stop here and leave the work to the new session`. fairshare never stops a session it
did not start, so the old agent must stop itself.

Either way the fresh session takes the old one's place: `status`, `watch`, `checkpoint --request`
and `pickup <old id>` find the fresh one, and workers the old session started are now its
workers. It starts in the folder the old session started in, even when the old one's shell had
moved into another (a worktree, say). It is named after the old session, as Claude Code lists it
(`fairshare-6a`, or the name you gave it), with the next handoff number: `fairshare-6a-h2`, then
`fairshare-6a-h3`; when that name cannot be read, after the folder. A worker's fresh session
keeps the worker's name. A switch takes nothing over.

A pickup keeps the session's name: the launcher reads it while the session still runs and gives
it to the resumed one once the old one has stopped. Claude Code gives no two running sessions one
name, so a pickup that starts while the old session still runs (in the background, or a copy of
a session that was not stopped by its limit) takes the next number instead, `fairshare-6a-h2`; a
background pickup of a session Claude Code no longer lists is named `pickup-<first 8 of its id>`.
A worker keeps its own name.

**Handoff with no note** (`! fairshare handoff`, you, in the session) has the launcher stop the
session, resume it headless for one turn that writes the note (no tools), then start the fresh
session in the same terminal. Meanwhile one line shows what it is doing, with a spinner and a
clock: checking accounts, reading the session, thinking, writing the note (with its word count so
far), `c = skip`. If the note cannot be written (an error, no answer in 5 minutes, `c`, no
account that can work), it says why in one line and picks the session up instead. Handoff
refuses outside a session, and refuses no note in a session `fairshare` did not start (ask the
agent: `/fairshare handoff`).

**A switch** prints how to resume the old session (`fairshare run -a work -- --resume <id>`),
then starts the fresh one; the launch pause applies. It works only in a session started by
`fairshare`: anywhere else it says to `/exit` and run `fairshare`. Bare `fairshare` never starts
a session inside another.

**The model must be available.** An account whose own week for that model is spent is skipped.
When no account that can work has it, a terminal asks whether to start on the top account with
its default model or quit; without a terminal it refuses, naming `--model`.

`! fairshare ...` and `/fairshare ...` are the same commands by two routes:

| | `! fairshare ...` | `/fairshare ...` |
|---|---|---|
| runs | at once, in your shell | through the agent, which runs `fairshare ...` |
| costs | no model turn | one turn |
| when the account is out | works | does not (the agent cannot answer) |
| `handoff` with no note | the launcher has the session write it | the agent writes it and runs `fairshare handoff "<note>"` |

`fairshare transcript <id> [--tail N]` prints what a recorded session said (the last 40 messages
by default), words only: no tool calls, tool output or thinking. A `!` command you typed shows as
`user ran: <command>` and its output as `output:`, at most 20 lines. A record with no transcript
says `no transcript recorded for <id>`.

## Workers

Small, short work can go to a subagent as usual (fairshare tells subagents nothing). Big or long
work goes to a worker, which can be stopped, moved and picked up on its own. fairshare installs a
skill in the hub (`skills/fairshare/`) that teaches the main session all of this; you can also
run the commands yourself, from inside a session:

- `fairshare worker --name NAME --model M --effort E "<prompt>"` starts one on the best account
  that has that model's week left (`--account ID` to choose) in the current folder and prints
  `worker fs-watch-stops started on b (session 9f8e7d6c)`. The name is required and unique across
  every recorded worker; the skill has the agent use the task in a few words with a short project
  prefix. The main session chooses the model and effort (both required; `--effort none` for a
  model without effort levels); the skill tells it to fit them to the task. The worker's prompt
  starts with one line telling it to checkpoint when asked.
- `fairshare watch` waits until something concerns this session and prints one line per event:
  its account crossed 90% or 95%, a worker wrote a checkpoint (with the note's path), a worker was
  stopped by its limit (with when that limit resets, since Claude Code resumes the worker by
  itself then, and `fairshare pickup <id>` to move it now), a worker ended its turn and is
  waiting (`worker fs-watch-stops ended its turn and is waiting: fairshare transcript 5e55105e --tail 1`;
  when it left background tasks running, the line says how many: it wakes again when one ends),
  or a worker ended or went idle. A worker's turn end comes from its own `Stop` hook, which
  fairshare wires in the hub: Claude Code's list of sessions can call a worker busy for hours
  after it stopped. The main session keeps it running as a background command and starts it
  again after each event; `--timeout SECONDS` gives up with `nothing happened`.
- `fairshare checkpoint "<what is done, what is next>"` appends a dated note to this session's
  checkpoint file (in fairshare's state folder) and prints its path; a handoff's fresh session
  reads it first. `fairshare checkpoint --request <worker name | session | all>` asks workers for
  one (each hears it at its next step; `all` skips the workers watch saw end); `--show <worker or
  session>` prints a note.
- When the main session hands off, or is picked up under a new id, its workers follow it (watch
  and `checkpoint --request` reach them from the new session) and each worker hears it once at
  its next step: `fairshare: your main session is now <new id> (<its name>): it continued <old
  id>. Reach it there.` The name is the one Claude Code lists the new session under (messages
  between sessions go by name); without one, the id alone. A pickup under the same id says
  nothing.
- A worker stopped by its limit: wait for its reset, or `fairshare pickup <its session id>`
  resumes it on the best account, in the background, under its own name, with its model and
  effort. To move the main session itself: `fairshare pickup` (the same session) or `fairshare
  handoff "<note>"` (a fresh one).

## Warnings

When the session's account crosses 90%, and again 95%, of a window (once each per window and
session; a new window starts over), fairshare adds one line to the session's context at its next
step (Claude Code's `PostToolUse` and `UserPromptSubmit` hooks, which fairshare wires in the
hub). A main session reads, for example: `fairshare: account a is at 91% of its 5h window
(resets 11:00). Best other account: b (5h 10% · 7d 40%); b before c (score 1.20 vs 1.00: ...);
b: seven_day 40% used, resets in 50h; c: ... If what is left of your work is small,
carry on. Otherwise, at the next stopping point: run fairshare handoff "<what was done, what is
next, ...>" to continue in a fresh session on the best account, which reads your note first.
Workers you started: ...`. That is the default move, `warning_move = "handoff"`: the fresh
session starts lean instead of reading the whole context again on another account, and it can read
the old session's transcript for anything the note missed. Set `warning_move = "pickup"` under
`[policy]` for the other: the line then says to write a checkpoint and run fairshare pickup, and
the same session continues on the best account with its whole context. (`! fairshare pickup`
works at any time, whichever is set; a session stopped by its limit is unchanged, see above.) A
worker reads that it should finish its current step and checkpoint, and that its main session
decides what happens next. Subagents hear nothing. Numbers older than twice `refresh_minutes`
warn nothing (fairshare asks for fresh ones instead). When one other session of the same account drew at least half of that account's
spend in the last hour, the main warning adds `Heaviest in the last hour: session 1f2e3d4c
(Opus, api, 70%).` The hook reads only local files; with nothing to say it prints nothing and
takes well under a tenth of a second.

## Who is drawing the quota

fairshare records what each session spends, from Claude Code's own running cost in the
statusLine (`cost.total_cost_usd`): an estimate at API list prices, so on a subscription it
compares sessions, it is not a bill. `fairshare status` lists, under each account, its sessions
seen in the last 10 minutes, heaviest in the last hour first (at most 5, then `+N more`):

```
> b   claude-pro   score  2.40  seven_day 40% used, resets in 25h
    session 1f2e3d4c · Opus · api · main · $6.00 (70%)
    session 9a8b7c6d · Haiku · api · fs-watch-stops · $1.10 (13%)
    session 4d5e6f70 · Sonnet · web · main · -
```

Each line: the session id's first 8 characters, its model, its folder, `main` or the worker's
name, its spend in the last hour and its share of the account's (`-`: no cost seen yet). When a
worker ends, or its cost has not moved for 30 minutes, its cost and duration are kept on its
worker record; nothing uses them yet.

## One assistant: the hub

Every account's folder in the hub links to one shared set of settings, CLAUDE.md, skills,
plugins, commands, agents, history, transcripts and project memory, so a session feels the same
whichever account it runs on. Before each launch fairshare checks the account's links and repairs
them without losing anything: a real folder Claude Code made is merged into the shared one
(clashing files keep both), new history lines are appended, a differing settings file is set
aside in `<hub>/backups/` with its diff shown, and it tells you in one line per item what it
changed. fairshare never deletes.

The usage hook lives in the hub's settings: `fairshare capture --then '<yours>'` records usage
for the account whose folder the session runs in, then runs your own statusLine and prints its
line.

**Moving the hub.** Move its folder, then run `fairshare doctor --fix` (it asks where the hub is
now, or takes `--hub PATH`). It changes `hub` in `accounts.toml`, relinks every account (the
links are relative, so they hold), rewires the usage hook if needed, and shows you every place in
the shared settings that names the old path (your own hooks, say), renaming them only after your
yes. Until then a launch stops with "no hub at <path>" instead of quietly building a new, empty
hub at the old path. On macOS the login lives in the keychain, one entry per config folder, so
after a move accounts may read as signed out: `doctor --fix` names `fairshare join ID` for each.

## Folder trust

Claude Code asks whether you trust a folder, and remembers the answer per account, so a folder
you trusted on one account asks again on the next. When you start a session on a terminal in a
folder that some account does not trust yet, fairshare asks once, before Claude Code starts, with
Claude Code's own warning (it will be able to read, edit and execute files there):
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

Without a terminal fairshare never creates trust, but it may copy it: `fairshare trust [PATH]`
(default: the current folder) gives every account that lacks trust for that folder the trust
another account already has, one line per account (`b now trusts /path (copied from a)`). It
refuses in your home folder, for a folder where you answered `[o]` only, and when no account
trusts the folder yet (then start a session there from a terminal and answer the question). A
worker start and a background pickup copy trust the same way to the one account about to start.

## Browser per account

If you keep each account in its own browser profile, join (and setup) asks which one opens that
account's sign-in: the default browser, one of your Chrome profiles (listed by name), or a
command. It is stored as one line in the account's table in `accounts.toml` (`browser =
"chrome:Profile 1"` or `browser = "command:/path/to/program"`); edit or remove that line to
change it. fairshare writes a one-line launcher for it in `<hub>/browsers/` and points `BROWSER`
at it for that account's sign-in and sessions. Whether `/login` inside a session honours
`BROWSER` is not verified.

## doctor

`fairshare -v` (or `--version`) prints the installed version and nothing else; it needs no
config.

`fairshare doctor` checks the setup and changes nothing in your folders or settings, nor in
fairshare's own config and state. It runs Claude Code's sign-in check and `/usage` in each
account's hub folder, which are Claude Code's own commands. It checks
that the `claude` command is found (with its version), `accounts.toml` loads, the hub is where it
is configured, and for each account: its plan exists, its sign-in check answers (with the date
the sign-in ends), its usage probe answers (or is off) and its `.claude.json` still holds folder
trust where fairshare expects it. Then: the hub's statusLine still calls capture, its four hooks
(stop-failure, post-tool, prompt, stop) still call `fairshare hook`, fairshare's skill in the hub is
current, and the `fairshare` command they call runs. Last, for each alias of yours that still
starts Claude Code on a folder joined into the hub, with no block of fairshare's after it to
override it, a `warn` line (`warn  claude: alias work in ~/.zshrc still starts Claude Code on
your own folder, not on work's in the hub (run `fairshare aliases`)`); a warning does not fail
doctor. One line per check, account ids, alias names and plan names only, so the output can go
into a public issue; exit 0 when all pass.

`fairshare doctor --fix` repairs what it can first (a moved hub, missing links, a stale or missing
hook, a missing or outdated skill, shown as a diff), one line per fix, then runs the checks.

## Uninstall

`fairshare uninstall` changes nothing (there is nothing to undo in your own folders). It prints:
- each shell file that holds fairshare's block of aliases, and that `fairshare aliases --remove`
  takes it out;
- where the config is, and the `accounts.toml.bak-fairshare-*` backups beside it (one per
  `join`, `leave` or `doctor --fix` that changed the file);
- where the state folder is: usage snapshots, session records and checkpoint notes;
- where the hub is and what it holds;
- that the hub's accounts stay signed in (on macOS, one keychain entry per folder), and that
  `fairshare leave --logout <id>` signs one out before you delete the hub;
- how to remove the installed fairshare (`pipx uninstall fairshare-cli`);
- a prompt for your own agent to merge the hub's history back into your own folders. It leaves
  out what join never copies (`sessions/`) and fairshare's own skill.

## accounts.toml

The config lives at `~/.config/fairshare/accounts.toml` (or `$FAIRSHARE_CONFIG`); fairshare's
state (snapshots, session records, checkpoints) at `~/.local/state/fairshare` (or
`$FAIRSHARE_STATE`). `setup`, `join`
and `leave` edit it as text, keeping your comments, with a backup each time. Keys you may edit:

- top level: `hub`, the hub's path (`doctor --fix` changes it after a move);
- per account (one `[[account]]` table each): `id`, `plan` (a plan file's name), `provider` (optional while
  there is one), `capacity` (overrides the plan's;
  kept when the detected plan differs), `browser` (see [Browser per account](#browser-per-account)),
  `status_args`, `usage_args`, and `from` (the folder `join --from` copied, written by join: it
  matches your aliases to the account, see [Your own aliases](#your-own-aliases));
- `[policy]`: `launch_pause` (seconds, default 3, `0` = off), `resume_wait_minutes` (unset by
  default: a session stopped by its limit waits for Claude Code's own resume; a number N moves
  it automatically when the reset is more than N minutes away, `0` = always), `warning_move`
  (`"handoff"` by default, or `"pickup"`: the move a 90% / 95% warning tells the agent to make, see
  [Warnings](#warnings)), `refresh_minutes`
  (default 5, at least 1), and the ranking's thresholds `short_hot_pct` (85: a short window this
  full is "nearly full"), `long_exhausted_pct` (98: a week or a short window this full is spent
  and cannot take work until it resets) and `grace_hours` (1: a nearly full, not spent, short
  window resetting this soon still takes work, when no other account can). Every number must be finite; `nan` and `inf` are a config error.

`status_args` replaces the sign-in check's arguments and `usage_args` the usage probe's (default
`auth status --json` and `-p --no-session-persistence --output-format json --settings
'{"disableAllHooks":true}' /usage`). Each takes a string, split like a shell would, or a list of
strings; anything else is a config error naming the key and the account. `usage_args = []` turns
the probe off. Without `--output-format json` fairshare can no longer tell whether the answer came
from the model.

## Exit codes

| code | when |
|---|---|
| 0 | done; for `run`, the session's own exit code is passed through (for `run -a ID <subcommand>`, Claude Code's) |
| 1 | an error: a config error, no hub at the configured path, no accounts yet, Claude Code not installed, an account that cannot be used, a failed `doctor` check |
| 2 | a usage error: an unknown command or account id, `setup` or `join` without a terminal, `full-reset` on an account whose current week is not known, one of Claude Code's own subcommands without `-a` |
| 75 | no account can take work and there is no terminal to ask (or you chose not to wait) |
| 126, 127 | Claude Code could not be started (126) or was not found (127) |
| 128 + N | the session died of signal N |
| 130 | Ctrl-C while waiting for an account |

## Adding a provider or plan

- **Plan:** drop a TOML file in `src/fairshare/plans/` (windows and relative capacity; the short
  window is optional). Adding, removing or renaming a subscription changes only plan files; an
  account that still names a missing plan is left out with a message.
- **Provider:** implement the protocol in `providers/base.py` in a new file under
  `src/fairshare/providers/` and register it. Everything the vendor may rename is a named
  constant in that file. The policy never changes.
