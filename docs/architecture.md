# Architecture

```
statusLine / provider output ──► providers/<p>.parse_capture ──► store (snapshot per account)
providers/<p>.read_usage (status, run: every account, in parallel) ──►┤ (same merge)
                                                                   │
accounts.toml ──► model.load_accounts ───────────────┐             ▼
plans/*.toml  ──► model.load_plan                    └──► policy.rank(accounts, snapshots, now)
                                                                   ▲   │
providers/<p>.account_status (once per command, in parallel) ──────┘   │
   signed in? subscription? same login as another folder? plan? ends?  ▼
                                                  cli: status | run ◄──┘
                                                       run ──► hub.present (else stop: moved?)
                                                           ──► providers/<p>.trusts / trust (ask)
                                                           ──► hub.check (the account's links)
                                                           ──► browser.env, providers/<p>.launch
                                                           ──► session.run_child (a child, not exec)

statusLine ──► capture ──► store.note_session (sessions/<id>.json), bar (+ `out` mark)
                      └──► stale? ──► detached `fairshare probe <id>` ──► read_usage ──► store
StopFailure (rate_limit) ──► hook stop-failure ──► stranded record, probe, SIGUSR1 ──► launcher
launcher (run_child) ◄── SIGUSR1 ──► confirm (survey) ──► stop child ──► countdown ──► run (pickup)

setup ──► join ──► hub.check, browser (ask), providers/<p>.login_command, account_status
                   [--from: hub.seed, providers/<p>.merge_state] ──► accounts.toml (+ one table)
doctor --fix ──► hub.present / accounts.toml `hub` ──► hub.check ──► wire ──► repath (ask)
```

## Seams

| Seam | What varies | Where |
|---|---|---|
| Provider | how usage is read, how the tool starts, where accounts already live | `providers/` (one file each) |
| Plan | window names and lengths, relative capacity | `plans/*.toml` (data only) |
| Policy | the routing rule | `policy.py` (pure function) |
| Storage | where snapshots live | `store.py` |
| Session | how a child session is started and supervised | `session.py` (provider-neutral) |
| Hub | where the shared items live and how each account's folder links to them | `hub.py` (provider-neutral; the share list is the adapter's `SHARED`) |
| Browser | where an account's sign-in links open | `browser.py` (provider-neutral; `BROWSER` names one launcher per account) |

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
   session starts. A session stopped by its account's limit is not moved: a new session on
   another account continues its work from the transcript (decision 19, pickup).
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
   - per-model limits are not plan data: they come from the provider's usage report (decision
     18), for whatever plan reports them.

   The "Public surface" table below lists every outside fact fairshare depends on today.

9. **One assistant: the hub.** Several accounts feel like one assistant (the same settings,
   CLAUDE.md, skills, plugins, commands, agents, history, transcripts and project memory) without
   fairshare writing into the user's own folders. Nothing fairshare does writes there any more;
   `init`, `apply`, the per-account statusLine wiring and `uninstall`'s file editing are gone.
   - **Layout.** One folder, by default `~/.local/share/fairshare/hub`, its path the top-level
     `hub` key of `accounts.toml`. `<hub>/shared/` holds the shared items; `<hub>/accounts/<id>/`
     is one config folder per account, holding what is its own (the login the provider creates
     there, its state file, caches, jobs, locks) and a RELATIVE link for every shared item
     (`projects -> ../../shared/projects`), so the whole hub can be moved: move the folder, change
     `hub` (`fairshare doctor --fix` does it, decision 17), and every link still holds. The
     hub also holds `browsers/` (one launcher per account with a `browser` line). `<hub>/backups/` gets whatever fairshare moves aside;
     fairshare never deletes. An account's folder is derived (`<hub>/accounts/<id>`), so an
     account table has no `config_dir` (an old one is a `ConfigError` that says to run `setup`),
     and an id must be a plain folder name.
   - **The launcher redirects.** A session on `<id>` runs with the provider's folder variable set
     to `<hub>/accounts/<id>/`. The user's own folders and aliases keep working untouched;
     sessions started by an old alias are simply not in the hub.
   - **Share list** (`SHARED` in the adapter; a name ending in `/` is a folder). Anything not on
     it stays per account. Every shared folder is created in `shared/` before linking, so the
     provider never makes a local one; a shared file that does not exist yet is linked anyway
     (the provider creates it through the link).
   - **Start check, every launch, lossless** (`hub.check`, under a lock on the hub; stage 3b's
     `doctor --fix` reuses it). A missing link is created. A real folder where a link belongs is
     merged into `shared/` (a clashing file keeps both: the incoming one is renamed
     `<name>.from-<id><ext>`; an identical one is not placed twice), then the rest of it goes to
     `backups/` and the link is made. A real `.jsonl` file (history) gives `shared/` the lines it
     lacks, in order. Another real file becomes the shared one when `shared/` has none; otherwise
     `shared/`'s is kept, the account's goes to `backups/`, and a difference is shown as a diff.
     A link pointing anywhere else (an absolute one to the right place included) is itself moved
     to `backups/`. One line per item says what changed (new links of one run share one line).
     If the hub cannot be changed, the launch stops (exit 1) rather than start a session that
     would write a local folder. Diffs go through `Provider.masked`: for Claude Code the values
     of the settings file's `env` are hidden.
   - **Settings and capture live in the hub.** `shared/settings.json` is fairshare's own file and
     carries the statusLine `<installed fairshare> capture`, wrapping a seeded statusLine with
     `--then` as before (`Provider.wire`, which changes only fairshare's own entries and keeps
     every other byte; any older capture form is replaced and keeps what it wrapped). The same
     `wire` adds `refreshInterval` to the statusLine when it has none and fairshare's own hook,
     `<installed fairshare> hook stop-failure` under `hooks.StopFailure` with matcher
     `rate_limit`, appended to that event's list (no other hook changed, moved or removed;
     recognised by its command, like the capture). setup, join and `doctor --fix` wire all three
     with one diff. `capture`
     takes no account id: it finds the account whose hub folder is the session's folder variable
     (`Provider.session_dir`); a session outside the hub records nothing and still runs `--then`.
     An id after `capture` (what the per-account wiring wrote) is accepted and ignored, so the
     user's own folders' old statusLines keep printing their line.
   - **`join <id>` for an id already joined** signs it in again and changes nothing else: only
     the provider's sign-in in its folder (with its browser), then the sign-in check under the
     same identity rules (a login that is another account's says "one quota"). This is the fix
     for an expired or moved login; `run -a` offers it for a signed-out account on a terminal.
   - **`join [<id>] [--from <folder>]`** creates the folder with its links (the start check), runs
     the provider's own sign-in in it (`Provider.login_command`, interactive; fairshare never sees
     a token), then the sign-in check: not signed in or not a subscription login adds nothing;
     the same identity as a configured account says "same login as <x>: one quota" and asks
     before keeping it; a check that gives no answer adds it with the default plan and a warning.
     `--from` COPIES (never moves) the shared items of a user's folder into `shared/`, except
     `Provider.not_copied` (Claude Code: `sessions/`, the running sessions and their keys): folders
     merge as above, `.jsonl` lines are appended, and a single file is copied only when `shared/`
     has none (the first seed wins; a later difference is shown, not applied; a hub settings file
     that holds only fairshare's own statusLine counts as none). Then, after asking, the state
     keys `Provider.merge_state` allows (trusted projects, user MCP servers, the onboarding keys)
     go into the new account's state file, adding only what it lacks; identity, tokens and
     account fields never. The Claude in Chrome consent keys are in that same asked-for group
     (consent is per account, like trust). The prompt names counts and server names only. The source is never
     written. Default id: the folder's alias or name, else the lowest free `account-N`.
   - **`leave <id> [--logout]`** removes the account's table from `accounts.toml`; its folder and
     login stay. `--logout` runs the provider's sign-out in that folder first (a failure changes
     nothing).
   - **`setup`** asks the hub folder (default shown; not asked again once set), lists the user's
     folders that discovery finds and runs `join <id> --from <folder>` for each one picked (one
     already joined is marked and skipped). A folder qualifies only with its own state file, or as
     the provider's default folder with HOME's state file beside it, or when an rc alias names
     it: shared items alone (a folder of shared sessions) do not make an account. Each folder's
     label comes from the read-only sign-in check, all at once; the default folder is checked
     without the folder variable, as the provider itself runs there. A folder with no state file
     is not checked, since the check would create one in it: "(sign-in unknown)", as for a check
     that gives no answer.
   - **Browser per account.** An account may have a `browser` line: absent, the system default;
     `chrome:<profile folder>`, that Chrome profile; `command:<absolute path>`, an executable
     taking the link. setup and join ask once on a terminal (Chrome profiles listed by display
     name, read from Chrome's own `Local State`, those two fields only); changing it later is an
     edit of that line. fairshare writes a one-line launcher per account, `<hub>/browsers/<id>`
     (mode 755, rewritten by each launch when missing or changed) and sets `BROWSER` to its
     absolute path for that account's sign-in, sign-in again and sessions. The provider calls
     `BROWSER` with the link as its one argument and does not split a value with arguments, so
     it must name one program. Bare
     `fairshare` with no config offers it on a terminal; otherwise it prints how and exits 2.
   - **`uninstall`** changes nothing: it prints where the config, state and hub are and what the
     hub holds, the command that removes fairshare, and a prompt the user can give their own agent
     to merge the hub's history back into their own folders.
   - **Every file fairshare writes**: refused when it cannot be read or decoded (never taken for
     absent); a symlink is written through to its target (`store.write_with_backup`), except the
     hub's own links, which the start check manages; backed up first, written atomically, read
     back. `accounts.toml` is edited as text under a lock, re-read just before writing: a change
     touches only its own lines (`model.with_hub`, `with_account`, `without_account`, each
     checked by parsing the result), so comments survive. The provider's state file may be
     re-serialised (Claude Code rewrites it itself); fairshare takes the provider's own lock
     folder (`Provider.state_lock`) while writing it, keeps every key it does not add, and when
     the file changed after the user was asked, it shows the new list and asks again.

10. **Ask the provider who is signed in.** `Provider.account_status(account)` returns a neutral
    `AccountStatus` (`logged_in`, `usable`, `identity`, `plan`, `expires_at`) or `None` when the
    question could not be answered. For Claude Code it runs the installed binary as `claude auth status --json`
    with the account's `CLAUDE_CONFIG_DIR`, at most 5 s, in an environment scrubbed of every
    variable that could pick another login or carry another session's state
    (`claude.clean_env`, which the launcher reuses). Verified on Claude Code 2.1.321 (2026-11-09):
    the output is a JSON object with `loggedIn`, `authMethod`, `email`, `orgId`, `planTier` among
    others. Rechecked on 2.1.322 (2026-11-10): same keys; a signed-out folder prints its answer and
    exits 1, so the adapter reads the output and ignores the exit code. `cli.py` never names the command or a field; the adapter parses, and the parser is
    tested against recorded outputs in `tests/fixtures/<version>/`. A new Claude Code version means
    a new fixture folder, not new test code. Everything is held in memory: `identity` is a hash of
    org id and email, used only to tell whether two folders are one login; the email, org id and
    org name are never printed or stored. `expires_at` is `loginExpiresAt` (an ISO time; absent,
    null or without a zone gives None, silently): `run`, `run -a` and `status` print
    `<id>: sign-in ends <day> <time>; \`fairshare join <id>\` signs it in again` for a signed-in
    account whose sign-in ends within `LOGIN_WARN_DAYS` (3, in `cli.py`), and `doctor`'s sign-in
    line adds `until <local date>`. The usage probe's envelope carries the same time
    (`credential_expires_at`); it is not read: one source. How the answer is used, in each of
    `status` and `run`:
    - not signed in, or not a subscription login (`authMethod` other than `claude.ai`): left out
      of the ranking; `status` says why, `run` prints one line;
    - several folders with one identity are one quota: ranked once, by the member with the
      newest snapshot; `status` shows the others as "same login as <id>";
    - the detected plan beats `accounts.toml` for ranking and for the plan `join` writes; `status` marks a
      mismatch. Detection reads the plan files' `names` list (`"pro"`, `"max"`, `"ultra"`; only
      `"pro"` has been observed so far), never a table in code;
    - when the check fails (non-zero exit, timeout, bad JSON, no binary) it fails soft, but never
      onto a login nobody confirmed. Every successful check records, per account, in
      fairshare's state dir (`checks/<id>.json`), whether it was a subscription login and when:
      a yes/no and a time, nothing else. After a failed check an account counts as signed in
      only when its last recorded answer was yes; it then takes its plan from `accounts.toml`
      and is not grouped. Otherwise it is left out: "sign-in check failed and <id> was never
      confirmed as a subscription login". The login file is no evidence (stage 3b): on 2.1.322
      macOS a fresh sign-in writes none, the login lives in the keychain. The reason: the state a
      changed `auth status` leaves behind is exactly when an API-billed login could be taken for
      a subscription, so a guess is not good enough. `status` names the failure and the
      fallback; `run` prints one line. `run -a` follows the same rule (exit 1 when refused).
      A failed check alone still never stops a launch of an account that was confirmed.

    The command's arguments are an adapter constant (`auth status --json`). To change them
    without a release, give an account `status_args = "auth status --json"` in `accounts.toml`
    (one string, split like a shell would; per account, and `join`/`leave` leave it untouched).
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
    (exit 2); an account that is not a subscription login, or whose plan has no plan file, stops
    with exit 1; a failed sign-in check falls back as in decision 10. A signed-out account on a
    terminal is asked "Sign in now? [y/N]": yes signs it in again (decision 9) and, when the
    check then passes, starts it; no, or no terminal, stops with exit 1 naming
    `fairshare join <id>`. A plain launch that skips a signed-out account adds one hint line
    naming that command, and asks nothing (routing still works). Only a
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
    `run_child` and provider-neutral (a pickup reuses it, decision 19):
    - SIGINT and SIGQUIT do nothing in the parent. The terminal sends them to the whole
      foreground process group, so the child still gets them. They are no-op handlers, not
      SIG_IGN, which would be inherited by the child;
    - Ctrl-Z: the parent ignores SIGTSTP and waits with WUNTRACED. When the child stops, the
      parent stops itself with SIGSTOP, so the shell gets its prompt back and sees one stopped
      job; when the parent is continued (`fg`), it sends SIGCONT to the child and keeps waiting;
    - SIGTERM and SIGHUP to the parent are forwarded to the child, which the parent then awaits;
    - SIGUSR1 asks for a pickup (decision 19) when the caller gives a callback; otherwise it
      keeps its default. The wait sleeps on a pipe every signal wakes (`set_wakeup_fd`), so a
      request is seen at any moment and the callback runs in the wait, not in a handler;
    - the exit code is the child's, or 128 plus the signal number when it died of a signal;
      a command that cannot start gives 127 (not found) or 126 with one message.

13. **`doctor` is read-only; `doctor --fix` repairs.** One line per check, `pass` or `FAIL`, exit 0
    when all pass and 1 otherwise: the provider's command is found (with its version, from the
    adapter); `accounts.toml` loads (a config error is one FAIL line with the file's name and the
    reason, never a traceback); the hub is at its configured path (when not, one FAIL line and no
    sign-in check, which would create folders at the old path); each account's plan exists, its
    sign-in check answers (with the sign-in's end date when known), its usage probe answers
    (decision 18: the window names it read and a count of model weeks, never a value; FAIL with
    the reason; `usage probe off (usage_args = [])` passes), and its state file reads and, when
    it holds project entries, at least
    one uses the trust field (otherwise "trust field not found; Claude Code may have renamed
    it"); the hub's one settings file still calls capture, and its stop-failure hook still
    calls `fairshare hook`; the `fairshare` command it calls
    exists and runs (`--help`; with no hook wired, the installed one). It reads settings and
    changes nothing, and prints account ids and plan names only, so the output can go into a
    public issue. `--fix` is decision 17.

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
    runs the user's own line (`--then`). `setup` and `join` without a terminal exit 2 with a
    message; over an `accounts.toml` that cannot be parsed they stop and say to fix it or move it
    aside (it is never replaced).
15. **A preference is timed, never a pin.** `fairshare prefer <id> [--for 30m|3h|2d]` stores one
    (account, until) in the state folder; with no `--for` it lasts until the end of the local
    day, and the output says when it ends. While it lasts, the preferred account goes first if it
    is eligible (`policy.prefer`, pure) and `status` tags its row `[preferred until <time>]` (a
    text tag like `[avoid]`: symbols vary in width and a pin reads as permanent); otherwise the
    ranking stands and `run`/`status` say so.
    It ends on its own, so it cannot quietly become a permanent pin that stops routing by
    usage; `prefer --off` ends it early. `run -a` stays separate: one session, no refusal.
    It starts no session, so an agent can set it mid-session when the user asks.

16. **Folder trust at launch.** The provider stores folder trust per account (Claude Code:
    `projects[<path>].hasTrustDialogAccepted` in the account's `.claude.json`), so a folder
    trusted on one account asks again on the next. After `run` has picked the account and
    before the start check, on a terminal only, fairshare repeats the provider's own warning
    (it "will be able to read, edit and execute files here"), names the folder and offers
    `[a]` trust for all accounts lacking it (named), `[o]` only the account about to start, or
    `[n]` no, exit (exit 1, nothing written). Lacking = joined accounts with a plan whose state
    does not trust the canonical working folder by the provider's own lookup rule
    (`Provider.trusts`; for Claude Code, verified live on 2.1.322: the folder and its parents up
    to the git root, plus the main repository's root for a worktree; every parent outside a
    repository). It asks when the starting account lacks trust, and when only others lack it
    (then `[o]` reads "only the accounts that already trust it" and there is no `[n]`), unless
    this folder has a remembered "only" answer (`trust-only.json` in the state dir, written
    atomically); a remembered "only" yields as soon as a launch lands on an account that lacks
    trust there. There is no default: Enter asks again; end of input is no. `run -a <id>` asks
    about that account only (`[y]`/`[n]`) and remembers nothing.
    - **Never in HOME, never without a terminal.** In the canonical HOME fairshare asks nothing
      and writes nothing ("In HOME, fairshare does not ask; Claude Code's own per-session question
      applies": Claude Code keeps HOME trust per session by design). Subfolders of HOME are normal.
      Without a terminal nothing is asked or written and the provider asks for itself. A pickup
      in the launcher goes through this same offer; a background pickup (no terminal) uses only
      accounts that already trust the folder, as later workers will.
    - **The write** (`Provider.trust`) goes where the provider's own dialog writes it (Claude
      Code: the main repository's root inside a repository, else the folder): under the
      provider's lock folder (wait up to 10 s; one left over, untouched for 30 s, is taken over,
      as Claude Code itself does; otherwise nothing is written and the line says so), a file that
      cannot be read or decoded is refused, the old one is backed up into `<hub>/backups/`, only
      that key is set and every other key and value kept, the file is replaced atomically and
      read back. A missing state file is created holding only that entry. One line per account
      written. A write that fails does not stop the launch: the provider asks for itself.

17. **The hub can move; a launch never rebuilds it.** The start check creates whatever is
    missing, so after the user moved the hub a launch would have built an empty one at the old
    path. Now every launch (`run`, `run -a`), `status`, `join <id>` again and `doctor` first
    require the configured hub to hold `shared/` and `accounts/` (a launch also the account's
    folder) and otherwise stop with exit 1: "no hub at <path>; if you moved it, run `fairshare
    doctor --fix`". This also keeps the sign-in check from creating folders at the old path
    (Claude Code creates a state file in the folder it is pointed at). setup and a new join still
    create hubs. `doctor --fix [--hub PATH]` (doctor stays read-only without it):
    1. a hub missing at its path: asks where it is now on a terminal (or takes `--hub`), accepts
       only a folder holding `shared/` and `accounts/`, and changes the `hub` line of
       `accounts.toml` (that line only, locked, backed up, like join and leave);
    2. runs the start check for every account and prints its lines;
    3. rewires the usage hook, with its diff, when the shared settings' statusLine does not call
       the installed `fairshare` (it moved, say);
    4. when step 1 moved the hub, finds every string value in the shared settings naming the old
       path (the user's own hooks may) and shows the diff renaming it; applied only after a yes
       (backed up, through the hub's lock), otherwise listed by key path. Nothing else changes;
    5. after a move, runs the sign-in check and names, for each account signed out,
       `fairshare join <id>` to sign it in again: on macOS the login lives in the keychain, one
       entry per config folder, so a moved hub may read as signed out (UNVERIFIED).
    One line per fix (`fixed` or `FAIL`), then the read-only checks; exit 0 when everything ends
    fixed. With no terminal and no `--hub`, it fixes what needs no answer and lists the rest
    (exit 1).

18. **Usage at launch.** A snapshot moves only while a session on that account exchanges
    messages, so usage spent elsewhere (the web app, the desktop app, a session outside the hub)
    was invisible and an idle account's snapshot could be days old. Now `status`, `run` and
    `run -a` ask the provider for every account's usage, every time
    (`Provider.read_usage(account, now)`), in the same thread pool as the sign-in checks: the
    added wait is the slowest answer, not their sum. For Claude Code it is the tool's own
    `/usage`: `claude -p --no-session-persistence --output-format json --settings
    '{"disableAllHooks":true}' /usage` (`USAGE_ARGS`), run like the sign-in check (clean
    environment, stdin from /dev/null, the default folder without the folder variable, in the
    launch's folder), at most `USAGE_TIMEOUT` (6 s; measured 1.1-2.4 s). Verified on 2.1.322
    (2026-11-11): a local command, no model call (`local_command: "usage"`, `num_turns: 0`, zero
    cost and tokens), no transcript, nothing added to history.
    - **Every time.** There is no "skip when the snapshot is minutes old" at launch: the probe is
      cheap and a launch is where a wrong number costs most. (Since stage 5a a snapshot's time is
      truthful, decision 20; sessions refresh in the background instead.)
    - **Hooks off.** `-p` fires the user's SessionStart and SessionEnd hooks, once per account
      per launch. `disableAllHooks` through `--settings` stops them, and beats a folder's own
      `settings.json` that sets it false (verified 2.1.322, 2026-11-11).
    - **Recorded like a capture.** Each answer's windows go through `store.record` (same lock,
      same merge: a lower value for the same window is still refused, since usage never falls
      within one window), as a probe: the snapshot's time moves even when nothing changed. Then the snapshots are loaded and ranked as before; the policy does not
      change. Twins (one login in several folders) are each probed. `doctor` probes and records
      nothing. `run`'s wait loop re-surveys after each sleep, so it probes again at the reset.
    - **Parsing.** Only lines starting with a known label are read (`USAGE_SESSION`,
      `USAGE_WEEK`, `USAGE_MODEL_WEEK`): `N% used · resets <when>`, extra spaces and a missing
      "used" tolerated. `<when>` is `Mon D at H[:MM]am|pm (Zone/Name)` (2.1.322) or a bare
      `H[:MM]am|pm` (2.1.321); a named zone is read with `zoneinfo`, an unknown or missing one as
      local time. There is no year: the first candidate after `now - SAME_WINDOW_S`, so a
      printed minute that jitters past the reset is not taken for next year's (or, for a bare
      clock time, tomorrow's). A line whose reset cannot be read is dropped: a window without
      one is useless to the policy. The session and all-models lines map to `five_hour` and
      `seven_day`, checked against the plan files as `parse_capture` does. The rest of the text
      (tips with local paths) is never parsed or kept.
    - **Per-model weeks** (`Current week (Fable)`) are kept under `model.model_window`'s key
      (`seven_day:Fable`), for any plan. The policy never reads them (owner decision,
      2026-11-09: model choice is not fairshare's business). When the chosen account's is at or
      above `long_exhausted_pct` and not reset, `run` and `run -a` print `warning: <id>'s <model>
      week is spent until <time>; other models are unaffected` and `status` shows it under the
      row. The bar shows one only at or above `short_hot_pct`, labelled by the model (`Fable 96%
      (Thu 03:00)`).
    - **The model-turn guard.** If a release stopped treating `/usage` as a local command, `-p
      "/usage"` would become a prompt and spend usage on every launch for every account. The JSON
      envelope shows it (`num_turns` or `total_cost_usd` above 0; a missing or other
      `local_command` alone is only "not understood", since an error answer may lack it): no
      windows are taken and `run`/`status` print one loud line each time naming `usage_args = []`
      as the way to stop it. There is no automatic off switch: the override is the off switch.
    - **Fallbacks.** A probe that fails (no answer: timed out, an exit code, no output, an error
      envelope; not signed in: the signed-out folder's cost summary; not understood: no window
      line) never stops a launch: `run` prints `usage probe failed for <id> (<reason>); using its
      snapshot from <time>` (or `no snapshot; ranked as a fresh window`, decision 6) for each
      ranked account, and `status` shows it under the row. A row whose numbers did not come from
      this run's probe shows `(usage as of <time>)`, the snapshot's `captured_at`.
    - **Override.** `usage_args` in an account's table, like `status_args`: a string split like
      a shell would, or a list of strings. `usage_args = []` turns the probe off for that
      account (no call, no failure line). Plain text without the envelope is parsed too, so an
      override without `--output-format json` still works, but then the model-turn guard is
      blind.

19. **Pickup first** (owner decision, 2026-11-11). Several sessions share one account's quota;
    on the first day of real use one account's five-hour window went from 23% to 60% in 13
    minutes, and the last 10% would take about four. A handoff written at a threshold cannot be
    relied on under that load, so when an account's limit stops a session, a fresh session on
    the best account continues the work from the transcript. Handoff becomes the cheaper path
    when there is time (stage 5c); percent warnings, `watch` and checkpoints are 5b.
    - **The session map.** capture keeps one small record per session in the state folder
      (`sessions/<id>.json`, atomic, under a lock): account, folder, transcript path, last seen,
      and when its limit stopped it. Written only when new or changed or last seen a minute ago;
      records unseen for 7 days are dropped at each write. The adapter maps the payload's
      `session_id`, `cwd`, `transcript_path` (`Provider.session_of`); nothing else names them.
    - **The signal.** `fairshare hook stop-failure` (wired under StopFailure, matcher
      `rate_limit`; one entry point for fairshare's hooks: reads the input, never raises, prints
      nothing, exits 0, since the provider ignores a StopFailure hook's output anyway) marks the
      session's record stranded (creating it if capture never saw it), starts the background
      probe for the account, and, when `FAIRSHARE_LAUNCHER` names one of its ancestors, writes
      the launcher's request file (`requests/<launcher pid>.json`: session, manual or not, the
      session's own process id) and sends it SIGUSR1. Only an ancestor: the variable leaks into
      everything the session starts and SIGUSR1's default action kills, so a stale or reused
      pid must never be signalled.
    - **The bar.** While the session's record is stranded and the policy says its account cannot
      take work, capture appends a red `out · ! fairshare pickup` (on its own line after the
      user's `--then` line). Numbers newer than the stop that say the account can work clear it.
    - **The launcher** exports `FAIRSHARE_LAUNCHER=<its pid>` to its child. On SIGUSR1 it reads
      the request (ignored unless it is for its own child's process). An automatic request is
      confirmed by a fresh survey with probes: the session's account must be ineligible;
      otherwise (a passing 429) it goes back to waiting and prints nothing, since the session
      owns the terminal. A manual request (`! fairshare pickup`) skips that check. Then it
      SIGTERMs the child (a clean exit was verified on 2.1.321 in about a second; SIGKILL after
      `STOP_WAIT`, 10 s), and for an automatic pickup on a terminal shows `<old> hit its limit
      (<window> resets <clock>). Continuing on <best> in 10 s · Enter = now · c = cancel` (keys
      read in cbreak mode, the terminal always restored; cancel prints `fairshare run -a <old>
      -- --resume <id>` and exits 0). Without a terminal it goes on at once. The pickup is the
      normal launch (trust offer, start check, child) in the session's folder on the top
      eligible account, with the pickup prompt as its first argument; none eligible is decision
      11's flow, carrying the prompt. The launcher stays the parent, so a second limit picks up
      again.
    - **The manual command.** `fairshare pickup [SESSION] [--bg]`. Inside a session with no
      SESSION (`CLAUDE_CODE_SESSION_ID`, read through the adapter): this session, through its
      launcher when it has one, else in the background. SESSION is a full id or a unique prefix
      of a recorded one (unknown or ambiguous: exit 2, listing the matches). From a terminal
      outside a session it runs in the foreground in the recorded folder (a gone folder is
      refused); inside a session or with `--bg`, in the background: `claude --bg --name
      pickup-<first 8> <prompt>` in the recorded folder, on the top eligible account that
      already trusts that folder (nothing can ask), with the account's clean environment and
      BROWSER, its output passed through (it names the attach command). None eligible and
      trusting: exit 1, naming which were eligible and which lacked trust. A session whose
      account can still work is picked up anyway, with one note line: moving is a valid wish.
    - **Never stopping others' sessions.** Only the launcher stops a session, and only its own
      child. Outside it, the old session is left as it is.
    - **The prompt** (cli text, provider-neutral): "This session continues session <id>, which
      stopped at <clock> because account <old> hit its usage limit. Its folder: <cwd>. Run
      `fairshare transcript <id> --tail 40` to see where it stopped; read the full transcript
      at <path> only if something is unclear. Then continue the work. First say in one line
      what you are picking up." A session moved by request says "which was stopped at <clock> on
      account <old> to move it" instead. `_pickup_prompt` keeps one place for a step that comes
      first (5b's checkpoint note).
    - **`fairshare transcript SESSION [--tail N]`** prints the main conversation's words, one
      block per message prefixed `user:` or `assistant:`, the last N (40). The adapter reads the
      file (`transcript_text`): only `user` and `assistant` records off the side chain, only text
      blocks; never tool calls, results, thinking, request copies or other records. A file it
      cannot read or with no message prints its path and says the layout was not understood
      (exit 1). Raw records are never printed.

20. **Freshness in session.** A snapshot's `captured_at` now means "when these numbers were last
    known to be current": it moves when a window's value or reset changed, a window appeared, or
    the numbers came from the `/usage` probe (the launch probe included); an idle redraw of
    equal numbers leaves it. A lower stale value is still refused.
    - `refresh_minutes` (in `[policy]`, default 5, at least 1; a smaller value is a
      `ConfigError`): how old an account's usage may get before fairshare asks `/usage` again.
    - **Background probe.** When capture runs for an account whose snapshot is older than that
      (or missing), it starts one detached `fairshare probe <id>` (hidden: `read_usage`, recorded
      as a probe, silent, exit 0; it checks the hub is there first), as this very fairshare
      (`<python> -P -m fairshare.cli`), with its own session, stdin and output on /dev/null, in
      the session's folder, never waited for: the bar does not slow down. At most one per account
      at a time: a start mark (`probes/<id>.started`) younger than 2 × USAGE_TIMEOUT means one is
      running. A probe that fails records nothing, so the bar ages honestly.
    - **The bar** shows `as of HH:MM` whenever the snapshot is older than `refresh_minutes`, as
      well as before a session's first exchange.
    - **`refreshInterval` = 60** on the statusLine (`STATUS_REFRESH_S`, added only when the
      user's statusLine has none): the bar re-reads the local snapshot once a minute at no cost,
      so a background probe's answer shows while the session is idle.

## Known limits

- Below the "nearly full" line, short-window usage plays no part in the ranking.
- A snapshot's age is read only to show it (`usage as of`); an account whose probe is off or
  failing is ranked on its snapshot until its windows reset.
- The usage probe's reset minute is approximate (it was seen to differ by a minute between runs
  and from the statusLine's exact `resets_at`), and its zone is as printed: an unknown zone is
  read as local time. Per-model weeks come only from the probe; the statusLine never carries
  them.
- The usage probe's "not signed in" is read from the cost summary a signed-out folder prints
  instead; an API-key login presumably prints the same (not verified), but such an account is
  already left out by the sign-in check.
- Idle sessions redraw their statusLine with old values. Within one window (reset times within
  an hour of each other) a lower used % is treated as stale and the stored one kept, so usage
  cannot roll back; an idle redraw of unchanged values leaves the snapshot's time (decision 20).
- An account at 100% of its short window that resets within `grace_hours` is eligible by the
  policy (for new launches). For a session the provider already stopped, pickup does not wait
  for that reset: a plan window spent right now confirms the pickup and shows the bar mark
  (`_spent_now`), and the countdown lets the user cancel and resume after the reset.
- Not yet checked live (planner, after merge): `!` commands inheriting
  `CLAUDE_CODE_SESSION_ID`; StopFailure firing on a real limit; `refreshInterval` re-runs (and
  whether they carry fresh `rate_limits`; fairshare assumes the last payload); `--bg` passing
  the environment through; `CLAUDE_PID` in a hook naming the session's own process (the
  launcher ignores a request carrying another pid).
- The ranking is greedy; it cannot promise full use of every week.
- The usage hook calls the installed `fairshare` by the absolute path found when `setup` or
  `join` last ran; if it moves (a reinstall elsewhere), `fairshare doctor` fails and
  `fairshare doctor --fix` (or `fairshare setup`) rewrites the hook (it rewrites any older
  capture).
- Whether Claude Code appends to `history.jsonl` in place (link kept) could not be provoked
  without a login; the start check repairs a replaced link losslessly either way.
- A shared settings change Claude Code writes by replacing the link (not seen: on 2.1.322 the
  theme picker wrote through the link) would be set aside at the next launch, with its diff.
- `plugins/` is shared as it is: install records seeded from a user's folder may name absolute
  paths inside that folder, so such plugins load from there until reinstalled in the hub.
- `join --from` copies every shared item but `sessions/`, so `tasks/` markers of a session
  running in the user's folder at that moment are copied too (Claude Code clears such leftovers
  itself, by its docs). `sessions/` is never copied: it holds each running session's key
  (observed 2.1.322: `<pid>.json` and `<pid>.<hash>.key`), stale on arrival and not ours to copy.
- `shared/` is one namespace: a second provider whose share list names the same item as Claude
  Code's would need its own folder there.
- The first folder copied decides the shared settings and CLAUDE.md; later differences are only
  shown. A first `join` without `--from` leaves only fairshare's statusLine, which a later seed
  replaces.
- `accounts.toml.lock` (beside the config) and `.lock` (in the hub) are lock files fairshare
  leaves in place.
- `join` cannot yet pick a provider: there is one.
- Claude Code creates `.claude.json` (and `backups/`) in a folder that lacks one when
  `auth status` runs there (docs and 2.1.322), and was seen leaving its `.claude.json.lock` folder
  behind in such a folder. setup therefore does not check a folder with no state file, and the
  trust write takes over a lock folder untouched for 30 s.
- The trust lookup rule was verified for plain folders, repositories and worktrees (2.1.322);
  a git submodule is treated as its own root, unverified.
- Whether `/login` inside a session honours `BROWSER` is UNVERIFIED; `claude auth login` does
  (2.1.322). Chrome profile launchers are macOS only (`open -na "Google Chrome"`).
- `BROWSER` from fairshare's own environment passes to an account with no `browser` line (a
  surrounding Claude Code session was seen to set it).
- `run -a` takes the account only as its first argument; later `-a` goes to the provider.
- Ctrl-Z and `fg` are tested by sending the stop and continue signals to a supervised fake
  child; a real terminal (interactive shell in a pty) was checked by hand once, 2026-11-09.
- Plan detection has seen only Claude Pro: the `"max"` and `"ultra"` names are unverified; an
  unmatched value leaves the plan in `accounts.toml`.
- The sign-in check relies on `claude auth status --json` (Claude Code 2.1.322). If a release
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
- Whether a moved hub reads as signed out (keychain entries per config folder) is UNVERIFIED;
  `doctor --fix` names `fairshare join <id>` for any account that then reads signed out.
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
(code 143, transcript intact) was verified on 2.1.321; the pickup relies on it (decision 19).

| Dependency | Adapter constant or function | Fallback when it changes | Verified |
|---|---|---|---|
| The tool on PATH (`claude`) | `BINARY`, `find_binary`, `real_binary` | launch stops with a clear message; `doctor` fails | 2.1.322, 2026-11-09 |
| Sign-in check: `auth status --json` and its fields (`loggedIn`, `authMethod`, `email`, `orgId`, `planTier`) | `STATUS_ARGS` (per account `status_args`), `STATUS_*`, `parse_account_status` | fails soft (decision 10): last confirmation and `accounts.toml` plan | 2.1.322, 2026-11-10; with the folder variable at a fresh empty folder and unset (temp HOME) both answer signed out, exit 1, differing only in `configDirectory`/`projectsDirectory`; both create the state file there |
| Default folder checked without the folder variable | `DEFAULT_DIR`, `account_status` | setup's label for it may be wrong | brief (2026-11-10); the probe above shows unset means `~/.claude` |
| Version: `--version` | `VERSION_ARGS`, `version` | `doctor` prints "version unknown" | 2.1.322, 2026-11-09 (prints `2.1.322 (Claude Code)`) |
| Account folder variable (`CLAUDE_CONFIG_DIR`) | `CONFIG_DIR_ENV`, `clean_env` | none: accounts could not be told apart | 2.1.321, 2026-11-09 (sign-in, sign-out and usage per folder, independently) |
| Login-override and session variables | `LOGIN_OVERRIDE_ENV`, `SESSION_ENV`, `SESSION_MARK_ENV`, `WATCH_ENV_PREFIX`, `passed_session_vars` | a variable missing from the lists reaches the child: add it (inside a session the launcher names unlisted `CLAUDE_*` ones) | session variables 2.1.322, 2026-11-09 (the names a running session injects into its children; inherited, they stop the child saving a transcript; scrubbing fixes it); login-override variables not tested live |
| Usage probe: `-p --no-session-persistence --output-format json /usage`, with `--settings '{"disableAllHooks":true}'` | `USAGE_ARGS` (per account `usage_args`), `USAGE_TIMEOUT`, `read_usage` | the probe fails soft (decision 18): the snapshot is used, one line per account; `doctor` fails | 2.1.322, 2026-11-11: signed in (planner, both accounts): exit 0 in 1.1-1.5 s, stderr empty, no model call, no transcript, no history line, no `projects` entry in the state file; hooks fire without the setting and not with it. Signed out (throwaway folder, temp HOME): exit 0, envelope with `is_error` false, `local_command` `usage` and a cost summary as `result`, no `credential_expires_at`, stderr empty; it creates the state file, `backups/`, `sessions/`, `plugins/store/` and an empty `projects/<folder>/memory/` there. Next to a folder `settings.json` with `disableAllHooks: false` and marker SessionStart/SessionEnd hooks: both fired without `--settings`, neither with it (command-line settings win) |
| Usage probe envelope: `result`, `is_error`, `local_command` (`usage`), `num_turns`, `total_cost_usd` | `USAGE_RESULT`, `USAGE_IS_ERROR`, `USAGE_LOCAL`, `USAGE_LOCAL_NAME`, `USAGE_TURNS`, `USAGE_COST` | turns or cost above 0 reads as "ran as a model turn": nothing taken, a loud line, `usage_args = []` named; an error answer is "no answer", a missing or other `local_command` "not understood" (neither proves the model ran) | 2.1.322, 2026-11-11 (`tests/fixtures/claude-2.1.322/usage-week-hot.json`, `usage-logged-out.json`) |
| Usage probe text: `Current session`, `Current week (all models)`, `Current week (<model>)`; `N% used · resets Mon D at H[:MM]am\|pm (Zone/Name)` (U+00B7), or a bare `H[:MM]am\|pm`; the signed-out `Total cost:` summary | `USAGE_SESSION`, `USAGE_WEEK`, `USAGE_MODEL_WEEK`, `USAGE_WINDOWS`, `USAGE_MONTHS`, `USAGE_COST_VIEW`, `parse_usage` | an unread line is dropped; no window line at all reads as "not understood" (the snapshot is used) | dated form 2.1.322, 2026-11-11 (fixtures `usage.txt`, `usage-week-hot.txt`; the minute jitters by one); clock-only form 2.1.321, 2026-11-09 (notes only, no fixture) |
| Sign-in end: `loginExpiresAt` in `auth status --json` | `STATUS_EXPIRES`, `parse_account_status` | no expiry warning, no date in `doctor` | 2.1.321, 2026-11-09 and 2.1.322, 2026-11-11 (equal to the usage envelope's `credential_expires_at` for the same folder; null when signed out) |
| Usage hook: the `statusLine` entry in `settings.json` and its payload (`rate_limits`, `used_percentage`, `resets_at`) | `SETTINGS_FILE`, `STATUS_LINE_KEY`, `HOOK_*`, `CAPTURE_*`, `wire`, `wired`, `parse_capture` | sessions record nothing (the launch probe still does); `doctor` shows the hook | 2.1.321, 2026-11-09 (documented fields; live capture on two accounts) |
| Share list: observed `projects/`, `history.jsonl`, `file-history/`, `paste-cache/`, `plugins/`, `skills/`, `tasks/`, `settings.json`, `sessions/`; documented user-level `CLAUDE.md`, `agents/`, `commands/`, `output-styles/`, `keybindings.json`, `rules/`, `workflows/`, `agent-memory/`, `themes/` | `SHARED` | an item missing from the list stays per account; one renamed leaves a stale link (harmless); `NOT_COPIED` (`sessions/`) is shared but never seeded | observed: 2.1.322 (real config dirs); documented: code.claude.com/docs/en/claude-directory, read 2026-11-09 (the last four, "Project and global" or "Global only" in its file reference: `rules/*.md`, `workflows/*.js`, `agent-memory/<name>/`, `themes/*.json`, read 2026-11-10) |
| Settings written through a symlinked `settings.json` | `SHARED` (the start check repairs either way) | a replaced link is repaired at the next launch, the difference shown | 2.1.322, 2026-11-09 (throwaway folder, first-run theme picker: link kept, target rewritten) |
| `history.jsonl` written through a link | `SHARED` | as above (lines merged) | not provoked: needs a login |
| Sign-in and sign-out: `auth login`, `auth logout` | `LOGIN_ARGS`, `LOGOUT_ARGS`, `login_command`, `logout_command` | `join` reports the sign-in did not finish; `leave --logout` changes nothing | documented (CLI reference, read 2026-11-09); not run live |
| State file `.claude.json` inside the folder variable's folder (beside `~/.claude` in HOME without it), its lock folder `.claude.json.lock` | `STATE_FILE`, `STATE_LOCK`, `STATE_LOCK_WAIT`, `STATE_LOCK_STALE`, `DEFAULT_DIR`, `state_file`, `hold_state` | `join --from` copies no state; the trust write says it could not | location: docs (claude-directory) and 2.1.322; lock: 2.1.322, 2026-11-10 (a folder, left behind by `auth status` in a folder whose state it created; a session wrote past a 25 s-old one and removed it) |
| Folder trust: `projects.<path>.hasTrustDialogAccepted`, canonical path; lookup: the folder and parents up to the git root, plus a worktree's main repository root; the dialog writes the main repository root inside a repository, else the folder | `STATE_PROJECTS`, `STATE_TRUST`, `GIT_DIR`, `GIT_FILE_PREFIX`, `GIT_COMMONDIR`, `trust_paths`, `trust_key`, `trusts`, `trust`, `check_state` | the trust question asks needlessly or the provider asks again; `doctor` fails when no entry uses the field | 2.1.322, 2026-11-10 (throwaway folders, `--bg` with no terminal: trusted starts, untrusted "Workspace not trusted"; the `/tmp` spelling of a `/private/tmp` folder refused; a parent trusted covers plain subfolders, not a repository or worktree below it; repo root trusted covers its subfolders and its worktree; a subfolder trusted covers itself and below, not the root; the dialog accepted in a repo subfolder and a worktree subfolder wrote the main repo root, in a plain subfolder that folder) |
| State keys `join` copies after asking: `projects.<path>.hasTrustDialogAccepted`, `mcpServers`, `hasCompletedOnboarding`, `lastOnboardingVersion`, `hasCompletedClaudeInChromeOnboarding`, `claudeInChromeDefaultEnabled` | `STATE_PROJECTS`, `STATE_TRUST`, `STATE_MCP`, `STATE_ONBOARDING`, `STATE_CHROME`, `merge_state` | the first-run screens, trust or Chrome prompts show again | 2.1.322, 2026-11-09: a fresh folder's first-run screen set none of them before sign-in (the theme went to `settings.json`); the binary sets the two onboarding keys together when onboarding finishes. Chrome keys: seen on a joined account whose first session asked for consent, 2026-11-10; not verified in a throwaway folder |
| `BROWSER` called with the sign-in link as its one argument, not word-split | `browser.ENV`, `browser.script`, `browser.env` | the sign-in prints the link to paste, as without a browser | `claude auth login`: 2.1.322 (brief, 2026-11-10); `/login` in a session: UNVERIFIED |
| Chrome's `Local State` (`profile.info_cache.<folder>.name`), `open -na "Google Chrome" --args --profile-directory=` | `browser.CHROME_LOCAL_STATE`, `CHROME_PROFILES`, `CHROME_NAME`, `CHROME_APP` | no profiles offered; a launcher that fails leaves the paste-the-link text | not run live (tests use fake data) |
| statusLine payload and common hook input: `session_id`, `transcript_path`, `cwd` | `SESSION_ID`, `SESSION_TRANSCRIPT`, `SESSION_CWD`, `session_of` | no session records: no bar mark, `pickup SESSION` finds nothing (the launch still works) | documented: code.claude.com/docs/en/statusline and /hooks ("Common input fields"), read 2026-11-11 |
| statusLine `refreshInterval` (seconds, minimum 1, on the statusLine object) | `STATUS_REFRESH_KEY`, `STATUS_REFRESH_S`, `wire` | the bar redraws on events only | documented: /statusline, read 2026-11-11; whether a re-run carries fresh `rate_limits` is unverified (fairshare assumes the last payload) |
| StopFailure hook: `hooks.StopFailure[{matcher: "rate_limit", hooks: [{type: "command", command}]}]`; input field `error` (`rate_limit`, `overloaded`, `authentication_failed`, ... `unknown`); output and exit code ignored | `HOOKS_KEY`, `HOOK_MATCHER_KEY`, `HOOK_LIST_KEY`, `STOP_FAILURE`, `RATE_LIMIT`, `HOOK_ERROR`, `FAIRSHARE_HOOKS`, `hook_of`, `hooked`, `hook_session` | no automatic pickup and no bar mark; `doctor` fails the hook line; `! fairshare pickup` still works | documented: /hooks, read 2026-11-11 (StopFailure input: `error`, optional `error_details`, `last_assistant_message`); hook table: 2.1.322 (planner). The wired entry passes the settings validator on 2.1.322, 2026-11-11 (`claude doctor`, throwaway folder and HOME under `env -i`, no login: no "Invalid settings"; a control with a numeric matcher was reported). Firing on a real limit: not yet checked live |
| Inside a session: `CLAUDE_CODE_SESSION_ID` (full id), `CLAUDE_PID` (the session's process) | `SESSION_ID_ENV`, `SESSION_PID_ENV`, `current_session`, `session_pid` | `pickup` with no SESSION says "not inside a session"; a request carries no pid (accepted) | tool shells: 2.1.322 (planner, 2026-11-11); `!` and hooks: not yet checked live |
| `--bg --name <name> <prompt>` (a session with no terminal, which prints how to attach), `--resume <id>` | `BG_ARGS`, `NAME_ARGS`, `RESUME_ARGS`, `background`, `resume_args` | background pickup fails with the tool's own message; the countdown's cancel hint is wrong | `--bg` 2.1.322 (trust row above); `--name` with an initial prompt and the environment passing through: not yet checked live |
| Transcript layout (internal, per version): one JSON record per line; `type` `user`/`assistant` with `isSidechain` false; `message.content` a string or blocks of which `text` ones are words | `T_*`, `transcript_text` | `fairshare transcript` prints the path and "layout not understood" (exit 1); the pickup prompt also names the full transcript path | 2.1.322, 2026-11-11 (planner's synthetic fixture `tests/fixtures/claude-2.1.322/transcript.jsonl`) |
| `claude agents --json` (live sessions with `pid`, `sessionId`, `cwd`, `kind`, `name`, `startedAt`, `status`; no account) | none: seen, unused | not applicable | 2.1.322 (planner, 2026-11-11) |
| Window names in the payload | `plans/*.toml` (`[windows.*] name`) | edit the plan file | `five_hour`, `seven_day`: 2.1.321, 2026-11-09 (docs and live capture) |
| Shell aliases that set the folder variable; the folders to look in (with their own state file, or the default folder with HOME's) | `RC_FILES`, `ACCOUNT_DIR_GLOB`, `STATE_FILE`, `discover` | `setup` lists fewer folders; `join --from <folder>` names one | not applicable |
| Plan names and capacity (`pro`, `max`, `ultra`) | `plans/*.toml` (`names`, `capacity`) | edit the plan file; an unmatched name leaves the config's plan | `pro` on 2.1.321; `max`, `ultra` unverified |

## Cold review 2 (commit 0f67c67): what happened to each finding

| Finding | Outcome |
|---|---|
| F1 expired fresh window overwrote the live one | fixed (`store.merge` never takes a window past its reset) |
| F2 non-UTF-8 settings.json treated as absent and replaced | retired by stage 3a: `apply`/`uninstall` file editing is gone; every file fairshare writes refuses what it cannot read (decision 9) |
| F3 unknown plan name broke every command, capture stopped | fixed: decision 14 |
| F4 concurrent captures rolled usage back | fixed (`store.record` holds a per-account lock) |
| F5 symlinked settings.json replaced by a file | retired by stage 3a (same reason); writes go through symlinks (decision 9) |
| F6 `apply` does not repair a hook at a moved `fairshare` | retired by stage 3a: `setup`/`join` rewrite any older capture with the installed command |
| F7 edit made while `apply` waits is overwritten | retired by stage 3a: writes re-read under a lock just before writing (decision 9) |
| F8 `uninstall` reformats the file and leaves `{}` | retired by stage 3a: `uninstall` changes nothing |
| F9 `usable_in` overstated the wait by the grace period | fixed (usable to the end of grace) |
| F10 tracebacks on bad input | fixed: decision 14 and decision 13; an unreadable settings.json in `apply` went with F2 |
| F11 fallback onto an unconfirmed login | fixed: decision 10 |
| F12 scrub dropped user settings, passed other variables | fixed: decision 12 |
| F13 detected plan dropped the capacity override | fixed: decision 2 |
| F14.3 `fairshare -p hi` | fixed: decision 12 |
| F14.4 `resets in 0h` | fixed: under an hour the reason reads in minutes (`resets in 9m`) |
| F14.7 `pick` has no caller | fixed by removing it |
| F14.8 `.claude*` literal in `discover` | fixed: `ACCOUNT_DIR_GLOB` |
| F14.1, F14.2 (init loses comments, signed-out folders not mentioned) | gone with `init` in stage 3a: config edits keep comments; `setup` lists signed-out folders, marked |
| F14.5, F14.6, F14.9 (`Suspended (signal)`, project-level statusLine, `model.py` reads files) | not part of this round; still open |
| §2 install needs network once | documented in the README |

## Not built yet

OpenAI/Codex adapter · handoff at a threshold (stage 5c) · percent warnings, `watch`, checkpoints
(stage 5b) · packaging for Homebrew.
