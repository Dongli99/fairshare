# Architecture

```
statusLine / provider output ──► providers/<p>.parse_capture ──► store (snapshot per account)
                                                                   │
accounts.toml ──► model.load_accounts ───────────────┐             ▼
plans/*.toml  ──► model.load_plan                    └──► policy.rank(accounts, snapshots, now)
                                                                   ▲   │
providers/<p>.account_status (once per command, in parallel) ──────┘   │
   signed in? subscription? same login as another folder? plan?        ▼
                                                  cli: status | run ◄──┘
                                                       run ──► providers/<p>.launch
                                                           ──► session.run_child (a child, not exec)
```

## Seams

| Seam | What varies | Where |
|---|---|---|
| Provider | how usage is read, how the tool starts, where accounts already live | `providers/` (one file each) |
| Plan | window names and lengths, relative capacity | `plans/*.toml` (data only) |
| Policy | the routing rule | `policy.py` (pure function) |
| Storage | where snapshots live | `store.py` |
| Session | how a child session is started and supervised | `session.py` (provider-neutral) |

## Decisions

1. **Provider-neutral windows.** The model has a *short* and a *long* window per plan; a
   plan file names the provider's own window for each role, and may omit the short one. A
   provider with daily/monthly windows fits without touching the policy.
2. **Capacity is data.** Percentages from different plan sizes are not comparable, so each
   plan declares a relative capacity (Pro = 1). Override per account in `accounts.toml`; the
   override is kept when plan detection swaps the plan (`Account.capacity`). The
   Claude values are Anthropic's published weekly multiples (Max 5x, Ultra 10x; see the plan
   files); Ultra reports no five-hour window when verified, and a missing window counts as
   unused (a temporary one on an unverified account is honoured).
3. **Real usage only.** Percentages come from the provider. Estimating limits from token counts
   was measured as unreliable (reports of ±37 points) and is out of scope.
4. **Start-time routing.** A running session is bound to one account. Routing happens when a
   session starts. Mid-session handover is a possible later add-on, not a goal.
5. **Pure policy, no I/O.** The rule takes accounts, snapshots and a clock, so every case is a
   unit test.
6. **No data is not zero.** An account with no snapshot is treated as a fresh window and ranked
   by the normal formula. It is neither favoured nor starved.

7. **One ordering, not tiers.** The first version split accounts into eligible and blocked and
   sorted the blocked by their short reset. A cold review showed that threw the score away
   across the boundary and mis-ranked spent weeks. Now a nearly-full short window scales the
   score (a soft preference) and a spent week sets it to `-hours until reset` (a hard fact,
   below any usable account). `Verdict.eligible` survives only so the CLI can report it.

8. **Volatile facts live in one place.** A provider renames commands, flags, environment
   variables, files, JSON fields, window names and plans, and adds or drops subscriptions. Each
   such fact is changed by editing one named constant or one data file, and nothing else:
   - facts about the provider's tool (binary name, command arguments, environment variable
     names, file and folder names, JSON field names, output formats) are named constants in
     `providers/<p>.py`. Nothing outside the adapter spells one: `cli.py` reaches a provider only
     through the `Provider` protocol in `providers/base.py`, and a test fails if `cli.py` names
     the Claude adapter's constants;
   - plan facts (names, capacity, window names, the provider's own plan names) are data in
     `plans/*.toml`. Adding, removing or renaming a subscription is a plan-file change only, and a
     plan may omit `[windows.short]` (it then ranks on the weekly window alone). The window names
     a usage payload may carry are read from the plan files too;
   - per-model limits are not plan data: they will come from the provider's usage report, for
     whatever plan reports them.

   The "Public surface" table below lists every outside fact fairshare depends on today.

9. **Setup edits, shown and reversible.** `wire()`/`unwire()` in the provider are pure: current
   file texts in, new texts out. The CLI shows each change as a diff, asks per file, and backs
   the file up before writing. The only thing `apply` edits is each account's usage hook (for
   Claude Code, its `statusLine`); fairshare writes no PATH entry, alias, shim or rc line.
   Reversal needs no state file: a wrapped hook carries the user's command after `--then`. An
   existing hook is never replaced, only wrapped. Any fairshare capture counts as fairshare's to
   remove, whatever form of the command an older version wrote. The command `apply` writes is
   the installed `fairshare`, found on PATH and written by absolute path (never a source path or
   `python -c`); with none installed, `apply` stops and says to install it first. Stage 3's hub
   replaces this wiring.

10. **Ask the provider who is signed in.** `Provider.account_status(account)` returns a neutral
    `AccountStatus` (`logged_in`, `usable`, `identity`, `plan`) or `None` when the question could
    not be answered. For Claude Code it runs the installed binary as `claude auth status --json`
    with the account's `CLAUDE_CONFIG_DIR`, at most 5 s, in an environment scrubbed of every
    variable that could pick another login or carry another session's state
    (`claude.clean_env`, which the launcher reuses). Verified on Claude Code 2.1.321 (2026-11-09):
    the output is a JSON object with `loggedIn`, `authMethod`, `email`, `orgId`, `planTier` among
    others. `cli.py` never names the command or a field; the adapter parses, and the parser is
    tested against recorded outputs in `tests/fixtures/<version>/`. A new Claude Code version means
    a new fixture folder, not new test code. Everything is held in memory: `identity` is a hash of
    org id and email, used only to tell whether two folders are one login; the email, org id and
    org name are never printed or stored. How the answer is used, in each of `status` and `run`:
    - not signed in, or not a subscription login (`authMethod` other than `claude.ai`): left out
      of the ranking; `status` says why, `run` prints one line;
    - several folders with one identity are one quota: ranked once, by the member with the
      newest snapshot; `status` shows the others as "same login as <id>";
    - the detected plan beats `accounts.toml` for ranking and `init`'s proposal; `status` marks a
      mismatch. Detection reads the plan files' `names` list (`"pro"`, `"max"`, `"ultra"`; only
      `"pro"` has been observed so far), never a table in code;
    - when the check fails (non-zero exit, timeout, bad JSON, no binary) it fails soft, but never
      onto a login nobody confirmed. Every successful check records, per account, in
      fairshare's state dir (`checks/<id>.json`), whether it was a subscription login and when:
      a yes/no and a time, nothing else. After a failed check an account counts as signed in
      only when its login file exists (existence only, never opened) AND its last recorded
      answer was yes; it then takes its plan from `accounts.toml` and is not grouped. Otherwise
      it is left out: "sign-in check failed and <id> was never confirmed as a subscription
      login" (or "not signed in" when there is no login file). The reason: the state a
      changed `auth status` leaves behind is exactly when an API-billed login could be taken for
      a subscription, so a guess is not good enough. `status` names the failure and the
      fallback; `run` prints one line. `run -a` follows the same rule (exit 1 when refused).
      A failed check alone still never stops a launch of an account that was confirmed.

    The command's arguments are an adapter constant (`auth status --json`). To change them
    without a release, give an account `status_args = "auth status --json"` in `accounts.toml`
    (one string, split like a shell would; per account, so `init` rewrites it untouched).
    Parsing stays code, so an override can change the command, not the output format.

11. **`run` does not launch an account that cannot take work.** If the top verdict is eligible it
    launches as before (with one warning line when it is eligible only because its hot short
    window resets within the grace period). If none is eligible it prints one line per account
    (reason, and when it is usable again, from `Verdict.usable_in`) and exits 75. On a terminal
    it offers `[w]ait` for the soonest account, `[s]tart` on the top-ranked one after a warning,
    or `[q]uit` (the default). Waiting sleeps to the target time in short naps, then checks
    every account again and re-ranks from fresh snapshots: it never launches on the old
    ranking. Ctrl-C exits 130 without launching. Exit 75 for no terminal stays. There is no
    `pick` command: nothing called it once the shim was gone.

12. **The launcher.** `fairshare` with no arguments starts a session on the best account;
    `fairshare run [args...]` does the same and passes `args` to the provider unchanged (it takes
    a prompt as its first argument, so there is no positional account id). Both go through
    decision 11. There is no option to name the launcher after the provider's own command and
    no PATH entry or alias: a user who wants a shorter name makes their own alias.
    `fairshare run -a <id> [args...]` (`--account`) starts that account and skips ranking and the
    refusal: it is the user's explicit choice, so a near-limit account gets one warning line
    (reason, when it is usable) and then launches. An unknown id lists the configured ones
    (exit 2); an account that is not signed in, not a subscription login, or whose plan has no
    plan file, stops with exit 1; a failed sign-in check falls back as in decision 10. Only a
    leading `-a`/`--account` is fairshare's; `run -- -a x` passes `-a x` on. Any other first word
    that is not a command (`fairshare -p hi`) exits 2 with `did you mean: fairshare run -p hi`.

    The session is a child process (`session.run_child`), not an `exec`, started with the
    provider's clean environment (decision 10, plus the account's own variable). The scrub is
    exact: two named lists in the adapter and nothing else. Session state a running session
    injects into its children is dropped silently (`SESSION_ENV`); login and provider overrides
    are dropped and the launcher prints one line naming each one it dropped (`LOGIN_OVERRIDE_ENV`;
    names only, never values); every other variable passes through, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
    included. A prefix rule was tried first and dropped user settings without a word. Its price
    is that a new session variable in a later release would pass through, so as a safety net,
    when the launcher itself runs inside a session, it prints one line naming any other
    `CLAUDE_*` variable it passes on (`passed_session_vars`), and the fix is to add the name to
    a list. Model and effort are not fairshare's business: nothing is passed. The signal rules, all in
    `run_child` and provider-neutral so a later handoff can reuse it:
    - SIGINT and SIGQUIT do nothing in the parent. The terminal sends them to the whole
      foreground process group, so the child still gets them. They are no-op handlers, not
      SIG_IGN, which would be inherited by the child;
    - Ctrl-Z: the parent ignores SIGTSTP and waits with WUNTRACED. When the child stops, the
      parent stops itself with SIGSTOP, so the shell gets its prompt back and sees one stopped
      job; when the parent is continued (`fg`), it sends SIGCONT to the child and keeps waiting;
    - SIGTERM and SIGHUP to the parent are forwarded to the child, which the parent then awaits;
    - the exit code is the child's, or 128 plus the signal number when it died of a signal;
      a command that cannot start gives 127 (not found) or 126 with one message.

13. **`doctor` is read-only.** One line per check, `pass` or `FAIL`, exit 0 when all pass and 1
    otherwise: the provider's command is found (with its version, from the adapter);
    `accounts.toml` loads (a config error is one FAIL line with the file's name and the reason,
    never a traceback); each account's plan exists and its sign-in check answers; the `fairshare` command a usage hook calls exists and runs
    (`--help`; with no hook wired, the installed one); each account's usage hook still calls
    capture. It reads settings and changes nothing, and prints account ids and plan names only,
    so the output can go into a public issue. A repairing `--fix` waits for stage 3, when
    everything it would repair is fairshare's own.

14. **Bad input degrades, it does not crash.** `load_accounts` raises only `ConfigError`, whose
    message says what is wrong and where (a TOML error with its line, a missing account key, an
    unknown or non-numeric `[policy]` key with the valid ones); the CLI prints it with the file's
    path and exits 1, and `doctor` reports it as a FAIL. An account whose plan has no plan file
    is not an error of the file: it loads with `plan` None and `plan_error` set ("plan <x> not
    found; known: <names>"), is left out of ranking (`status` says why, `run` prints one line,
    `doctor` fails that account only, `run -a` of it exits 1) and every other account works.
    Its provider is the one it names, else the only provider the plan files serve; with several
    and none named it is a `ConfigError`. `capture` needs only the provider's window names, so it
    keeps recording for every account, the broken one included, and a failing `capture` still
    runs the user's own line (`--then`). `init` without a terminal and without `--yes` or
    `--account` exits 2 with a message; over a broken `accounts.toml` it shows the error and asks
    whether to start fresh (the old file is backed up when the new one is written); `--yes` alone
    refuses to discard it unasked, `--account` replaces it.

## Known limits

- Below the "nearly full" line, short-window usage plays no part in the ranking.
- A snapshot's age is never read, so a stale snapshot is trusted until its windows reset.
- Idle sessions redraw their statusLine with old values. Within one window (reset times within
  an hour of each other) a lower used % is treated as stale and the stored one kept, so usage
  cannot roll back; an idle redraw of unchanged values still counts as fresh.
- The ranking is greedy; it cannot promise full use of every week.
- The usage hook calls the installed `fairshare` by the absolute path found when `apply` ran; if
  it moves (a reinstall elsewhere), `fairshare doctor` fails and `fairshare apply` is run again.
- `run -a` takes the account only as its first argument; later `-a` goes to the provider.
- Ctrl-Z and `fg` are tested by sending the stop and continue signals to a supervised fake
  child; a real terminal (interactive shell in a pty) was checked by hand once, 2026-11-09.
- Plan detection has seen only Claude Pro: the `"max"` and `"ultra"` names are unverified; an
  unmatched value leaves the plan in `accounts.toml`.
- Per-model weekly limits (e.g. a Fable week, seen on Pro as well) are not read yet; they will
  come from the provider's usage report for whatever plan reports them, never from plan data.
- The sign-in check relies on `claude auth status --json` (Claude Code 2.1.321). If a release
  changes it, the check fails soft (see decision 10) until a fixture and the parser follow.
- `claude --version` (for `doctor`) and the usage hook have not been checked against a recorded
  Claude Code version; see the table below.
- `capture` reads `accounts.toml` through the same loader, so a config error (including a bad
  `[policy]` key) also stops usage being recorded until it is fixed; `status`, `run` and `doctor`
  all report it, so it is not silent.
- The scrub lists are verified for Claude Code 2.1.322. A session variable a later release adds
  passes through (the launcher names it when run inside a session) until it is added to
  `SESSION_ENV`. `CLAUDE_CODE_USE_*` and `ANTHROPIC_BASE_URL` are dropped as login/provider
  overrides, so a user who relies on one of them for Claude Code gets the account's own login
  instead, with a notice.
- An account on a plan with no plan file is left out until `accounts.toml` or the plan file is
  fixed; a detected plan can only replace a plan that loaded.
- After a failed sign-in check, an account never confirmed as a subscription login is left out,
  even if it is one, until one check succeeds (`fairshare status` records it as soon as the check
  answers). A first run on a Claude Code release whose check already fails is therefore refused
  until the parser follows.
- `run` waits by sleeping; it does not watch for a snapshot a running session writes meanwhile
  until it wakes and re-ranks.

## Public surface

Every outside fact fairshare relies on today, where the adapter names it, what happens when it
changes, and the Claude Code version it was last verified on. "Not recorded" means it has worked
since it was written but no version was noted; `doctor` is the check. A clean exit on SIGTERM
(code 143, transcript intact) was verified on 2.1.321 for stage 5's handoff; nothing relies on it yet.

| Dependency | Adapter constant or function | Fallback when it changes | Verified |
|---|---|---|---|
| The tool on PATH (`claude`) | `BINARY`, `find_binary`, `real_binary` | launch stops with a clear message; `doctor` fails | 2.1.322, 2026-11-09 |
| Sign-in check: `auth status --json` and its fields (`loggedIn`, `authMethod`, `email`, `orgId`, `planTier`) | `STATUS_ARGS` (per account `status_args`), `STATUS_*`, `parse_account_status` | fails soft (decision 10): login file and `accounts.toml` plan | 2.1.321, 2026-11-09 |
| Version: `--version` | `VERSION_ARGS`, `version` | `doctor` prints "version unknown" | 2.1.322, 2026-11-09 (prints `2.1.322 (Claude Code)`) |
| Account folder variable (`CLAUDE_CONFIG_DIR`) | `CONFIG_DIR_ENV`, `clean_env` | none: accounts could not be told apart | 2.1.321, 2026-11-09 (sign-in, sign-out and usage per folder, independently) |
| Login-override and session variables | `LOGIN_OVERRIDE_ENV`, `SESSION_ENV`, `SESSION_MARK_ENV`, `WATCH_ENV_PREFIX`, `passed_session_vars` | a variable missing from the lists reaches the child: add it (inside a session the launcher names unlisted `CLAUDE_*` ones) | session variables 2.1.322, 2026-11-09 (the names a running session injects into its children; inherited, they stop the child saving a transcript; scrubbing fixes it); login-override variables not tested live |
| Usage hook: the `statusLine` entry in `settings.json` and its payload (`rate_limits`, `used_percentage`, `resets_at`) | `SETTINGS_FILE`, `STATUS_LINE_KEY`, `HOOK_*`, `CAPTURE_*`, `wire`, `unwire`, `parse_capture` | no usage is recorded; accounts rank as fresh; `doctor` shows the hook | 2.1.321, 2026-11-09 (documented fields; live capture on two accounts) |
| Window names in the payload | `plans/*.toml` (`[windows.*] name`) | edit the plan file | `five_hour`, `seven_day`: 2.1.321, 2026-11-09 (docs and live capture) |
| Login file (`.credentials.json`), existence only | `LOGIN_FILE`, `login_present` | only used when the sign-in check fails, and then only for an account last confirmed as a subscription login | 2.1.321, 2026-11-09 (present exactly in signed-in folders) |
| Shell aliases that set the folder variable; the folders to look in | `RC_FILES`, `ACCOUNT_DIR_GLOB`, `discover` | `init` finds fewer accounts; name them with `--account` | not applicable |
| Plan names and capacity (`pro`, `max`, `ultra`) | `plans/*.toml` (`names`, `capacity`) | edit the plan file; an unmatched name leaves the config's plan | `pro` on 2.1.321; `max`, `ultra` unverified |

## Cold review 2 (commit 0f67c67): what happened to each finding

| Finding | Outcome |
|---|---|
| F1 expired fresh window overwrote the live one | fixed (`store.merge` never takes a window past its reset) |
| F2 non-UTF-8 settings.json treated as absent and replaced | deferred to stage 3, which removes `apply`/`uninstall`'s file editing |
| F3 unknown plan name broke every command, capture stopped | fixed: decision 14 |
| F4 concurrent captures rolled usage back | fixed (`store.record` holds a per-account lock) |
| F5 symlinked settings.json replaced by a file | deferred to stage 3 (same reason as F2) |
| F6 `apply` does not repair a hook at a moved `fairshare` | deferred to stage 3 (same reason); the Known limit above still says to re-run `apply`, which does not repair it until then |
| F7 edit made while `apply` waits is overwritten | deferred to stage 3 (same reason) |
| F8 `uninstall` reformats the file and leaves `{}` | deferred to stage 3 (same reason) |
| F9 `usable_in` overstated the wait by the grace period | fixed (usable to the end of grace) |
| F10 tracebacks on bad input | fixed: decision 14 and decision 13; an unreadable settings.json in `apply` is part of F2 and deferred with it |
| F11 fallback onto an unconfirmed login | fixed: decision 10 |
| F12 scrub dropped user settings, passed other variables | fixed: decision 12 |
| F13 detected plan dropped the capacity override | fixed: decision 2 |
| F14.3 `fairshare -p hi` | fixed: decision 12 |
| F14.4 `resets in 0h` | fixed: under an hour the reason reads in minutes (`resets in 9m`) |
| F14.7 `pick` has no caller | fixed by removing it |
| F14.8 `.claude*` literal in `discover` | fixed: `ACCOUNT_DIR_GLOB` |
| F14.1, F14.2, F14.5, F14.6, F14.9 (init loses comments, signed-out folders not mentioned, `Suspended (signal)`, project-level statusLine, `model.py` reads files) | not part of this round; still open |
| §2 install needs network once | documented in the README |

## Not built yet

OpenAI/Codex adapter · stale-snapshot warning in `status` ·
mid-session handover · packaging for Homebrew.
