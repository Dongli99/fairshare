# fairshare

One assistant across the AI-assistant subscriptions you already have. Every account shares the
same history, memory, settings and skills, and each new session starts on the account whose
budget is best used right now, judged by its real usage. When an account runs out mid-task, the
session continues on another one with its context. The aim is to use every account fully before
its weekly reset. It is a heuristic, not a guarantee.

> Status: early (0.x). Claude Code on Pro, Max and Ultra plans is the first supported provider.
> Runs on macOS and Linux; Windows is not supported.

**Terms.** Whether spreading work across several subscriptions fits a provider's terms is for
you to check. [docs/terms-and-limits.md](https://github.com/Dongli99/fairshare/blob/main/docs/terms-and-limits.md) says what fairshare
deliberately never does and what is left for you to decide.

```
$ fairshare status
> two   claude-pro   score 2.40  seven_day 40% used, resets in 25h
  one   claude-pro   score 0.27  seven_day 70% used, resets in 111h

$ fairshare       # starts a session on the account at the top of that list
```

## Quick start

```
pipx install fairshare
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
- it does not touch your shell files: plain `claude` still uses `~/.claude`, outside the hub.
  Start sessions with `fairshare`.

## Everyday use

- `fairshare` starts a session on the best account. `fairshare run <args>` passes the arguments
  to Claude Code unchanged; `fairshare run -a ID` starts that account instead.
- `fairshare status` lists the accounts, best first, with their usage and live sessions.
- When an account hits its limit mid-session, fairshare checks it really is out. If the limit
  resets soon (30 minutes by default), Claude Code resumes the session by itself. Otherwise the
  same session continues on the best other account, with its whole context.
- Inside a session, `! fairshare pickup` moves it now, `! fairshare handoff` starts a fresh
  session from a written note, and `! fairshare` starts a fresh session with nothing carried
  over (a *switch*).
- A *worker* is a background session the main session starts for big work; fairshare warns the
  main session near a limit and it decides what its workers do. A skill in the hub teaches it.
- `fairshare doctor` checks the setup; `fairshare doctor --fix` repairs what it can.

Every command, message and setting is in [docs/usage.md](https://github.com/Dongli99/fairshare/blob/main/docs/usage.md).

## How it decides

For each account:

```
score = capacity × (100 − weekly used %) ÷ hours until the weekly reset
```

The score is the share of the week's budget it must burn per hour to use it all; the highest
score wins. That sends work to the account whose reset is nearest and whose budget is least
used. Capacity is the plan's size (Pro = 1).

- If the short (5-hour) window is nearly full and not resetting within the hour, the score is
  scaled by what is left of that window.
- An account whose week is spent ranks below every usable account.

Usage comes from Claude Code's own local `/usage` command, asked for every account at each
launch (no model call, nothing spent), and from its status line during a session. The rule is
one pure function in [`src/fairshare/policy.py`](https://github.com/Dongli99/fairshare/blob/main/src/fairshare/policy.py).

## Removal

```
fairshare uninstall        # changes nothing; says where everything is
pipx uninstall fairshare
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
