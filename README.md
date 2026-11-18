# fairshare

One assistant across the AI-assistant subscriptions you already have. Every account shares the
same history, memory, settings and skills, and each new session starts on the account whose
budget is best used right now, judged by its real usage. When an account runs out mid-task, the
session continues on another one with its context. The aim is to use every account fully before
its weekly reset. It is a heuristic, not a guarantee.

![A 30-second demo with three made-up accounts: fairshare status ranks them by real usage; fairshare starts a session on the best one; when that account reaches 95% of its 5-hour window, fairshare tells the agent, which writes a checkpoint and runs fairshare pickup, and the same session continues on another account](https://raw.githubusercontent.com/Dongli99/fairshare/main/docs/demo.svg)

> Status: early (0.x). Claude Code on Pro, Max and Ultra plans is the first supported provider.
> Runs on macOS and Linux; Windows is not supported.

**Terms.** Whether spreading work across several subscriptions fits a provider's terms is for
you to check. [docs/terms-and-limits.md](https://github.com/Dongli99/fairshare/blob/main/docs/terms-and-limits.md) says what fairshare
deliberately never does and what is left for you to decide.

## Quick start

```
pipx install fairshare-cli      # the command is `fairshare`
fairshare setup
fairshare
```

From source: `git clone https://github.com/Dongli99/fairshare && pipx install ./fairshare`.
Installing needs network access once, to fetch the build tool; fairshare itself has no
dependencies.

## What setup does

`fairshare setup` builds the **hub**: one folder (default `~/.local/share/fairshare/hub`) that
holds a Claude Code config folder per account, all linked to one shared set of history,
transcripts, memory, settings, CLAUDE.md and skills. Before you run it, know that:

- it lists the Claude Code folders it finds, asks which to join and, for each, a name (Enter
  keeps the folder's own);
- each account you join gets a new folder in the hub and **one browser sign-in** there, through
  Claude Code's own sign-in (fairshare never sees a token);
- it copies your history, settings and skills into the hub. Your own folders are not changed:
  fairshare never writes them, and to label them it runs Claude Code's own sign-in check there;
- **the first folder joined decides the shared settings, CLAUDE.md and keybindings**. Later folders'
  differences are shown, not applied. `all` joins signed-in folders first, your default
  `~/.claude` first among them; type numbers to choose the order yourself;
- it asks once whether to turn on Remote Control for every session (Enter: yes), unless your
  settings already say;
- it does not touch your shell files: plain `claude` still uses `~/.claude`, outside the hub.
  Start sessions with `fairshare`.

## Everyday use

- `fairshare` starts a session on the best account. `fairshare run <args>` passes the arguments
  to Claude Code unchanged; `fairshare run -a ID` starts that account instead.
- `fairshare status` lists the accounts, best first, with their usage and live sessions.
- Near a limit (90% and 95% of a window), fairshare tells the agent in the session, naming the
  best account now. A skill fairshare installs in the hub teaches it to write a checkpoint and run
  `fairshare pickup` at its next stopping point, unless little work is left: the session moves on
  before the limit, with its context. It is the agent's call, so it usually happens, not always.
- When an account hits its limit mid-session, fairshare checks it really is out, then leaves
  the session for Claude Code to resume by itself at the reset, with its whole context. A line
  and the status bar say which limit, when it resets, and the best account that can take work;
  `! fairshare pickup` moves the session there now. `resume_wait_minutes` makes that move
  automatic. A session is never resumed on an account that is out.
- Inside a session, `! fairshare pickup` moves it now, `! fairshare handoff` starts a fresh
  session from a written note, and `! fairshare` starts a fresh session with nothing carried
  over (a *switch*).
- A *worker* is a background session the main session starts for big work; near a limit, the
  main session decides what its workers do.
- `fairshare doctor` checks the setup; `fairshare doctor --fix` repairs what it can.
  `fairshare -v` prints the installed version.

Every command, message and setting is in [docs/usage.md](https://github.com/Dongli99/fairshare/blob/main/docs/usage.md).

## How it decides

For each account:

```
score = capacity × (100 − weekly used %) ÷ hours until the weekly reset
```

The score is the share of the week's budget it must burn per hour to use it all; the highest
score wins. That sends work to the account whose reset is nearest and whose budget is least
used. Capacity is the plan's size (Pro = 1).

- If the short (5-hour) window is nearly full, the score is scaled by what is left of that
  window, and new sessions go to an account that can work now. One resetting within the hour is
  still used when no other account can.
- Between accounts within 10% of each other, a new session goes to the one with fewer sessions
  running.
- An account whose week or short window is spent ranks below every usable account, however soon
  it resets: it cannot take work until then.

Usage comes from Claude Code's own local `/usage` command, asked for every account at each
launch (no model call, nothing spent), and from its status line during a session. The rule is
one pure function in [`src/fairshare/policy.py`](https://github.com/Dongli99/fairshare/blob/main/src/fairshare/policy.py).

## Removal

```
fairshare uninstall        # changes nothing; says where everything is
pipx uninstall fairshare-cli
```

`uninstall` lists the config, the state folder (usage snapshots, session records, checkpoint
notes) and the hub, and gives you a prompt for your own agent to merge the hub's history back
into your own folders. They all stay until you delete them. The hub's accounts stay signed in;
`fairshare leave --logout ID` signs one out before you delete the hub.

## Requirements

- macOS or Linux, Python 3.11+.
- Claude Code, signed in with a Pro, Max or Ultra subscription on each account.

## More

- [docs/usage.md](https://github.com/Dongli99/fairshare/blob/main/docs/usage.md): every command, message, setting and exit code.
- [docs/terms-and-limits.md](https://github.com/Dongli99/fairshare/blob/main/docs/terms-and-limits.md): what fairshare never does.
- [docs/architecture.md](https://github.com/Dongli99/fairshare/blob/main/docs/architecture.md): the design decisions, seams and known limits.

## Prior art

[ccm](https://github.com/revfactory/ccm) shares state between Claude Code accounts in a similar
way. fairshare borrowed several of its ideas, reimplemented in its own code: ignoring stale
statusLine redraws, reading `claude auth status`, warning about login-override variables, and
merging a stray folder into the shared one with the clashing file renamed.

## Contributing

See [CONTRIBUTING.md](https://github.com/Dongli99/fairshare/blob/main/CONTRIBUTING.md). New providers and plans are welcome; the policy never
changes for them.

## License

MIT
