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
                                                       run ──► hub.check (the account's links)
                                                           ──► providers/<p>.launch
                                                           ──► session.run_child (a child, not exec)

setup ──► join ──► hub.check, providers/<p>.login_command, account_status
                   [--from: hub.seed, providers/<p>.merge_state] ──► accounts.toml (+ one table)
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

9. **One assistant: the hub.** Several accounts feel like one assistant (the same settings,
   CLAUDE.md, skills, plugins, commands, agents, history, transcripts and project memory) without
   fairshare writing into the user's own folders. Nothing fairshare does writes there any more;
   `init`, `apply`, the per-account statusLine wiring and `uninstall`'s file editing are gone.
   - **Layout.** One folder, by default `~/.local/share/fairshare/hub`, its path the top-level
     `hub` key of `accounts.toml`. `<hub>/shared/` holds the shared items; `<hub>/accounts/<id>/`
     is one config folder per account, holding what is its own (the login the provider creates
     there, its state file, caches, jobs, locks) and a RELATIVE link for every shared item
     (`projects -> ../../shared/projects`), so the whole hub can be moved: move the folder, change
     `hub`, and every link still holds. `<hub>/backups/` gets whatever fairshare moves aside;
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
     `--then` as before (`Provider.wire`, which changes only the statusLine's command and keeps
     every other byte; any older capture form is replaced and keeps what it wrapped). `capture`
     takes no account id: it finds the account whose hub folder is the session's folder variable
     (`Provider.session_dir`); a session outside the hub records nothing and still runs `--then`.
     An id after `capture` (what the per-account wiring wrote) is accepted and ignored, so the
     user's own folders' old statusLines keep printing their line.
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
     account fields never. The prompt names counts and server names only. The source is never
     written. Default id: the folder's alias or name, else the lowest free `account-N`.
   - **`leave <id> [--logout]`** removes the account's table from `accounts.toml`; its folder and
     login stay. `--logout` runs the provider's sign-out in that folder first (a failure changes
     nothing).
   - **`setup`** asks the hub folder (default shown; not asked again once set), lists the user's
     folders that discovery finds (signed-out ones marked, by the login file's existence: nothing
     is run against them) and runs `join <id> --from <folder>` for each one picked. Bare
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
    `AccountStatus` (`logged_in`, `usable`, `identity`, `plan`) or `None` when the question could
    not be answered. For Claude Code it runs the installed binary as `claude auth status --json`
    with the account's `CLAUDE_CONFIG_DIR`, at most 5 s, in an environment scrubbed of every
    variable that could pick another login or carry another session's state
    (`claude.clean_env`, which the launcher reuses). Verified on Claude Code 2.1.321 (2026-11-09):
    the output is a JSON object with `loggedIn`, `authMethod`, `email`, `orgId`, `planTier` among
    others. Rechecked on 2.1.322 (2026-11-10): same keys; a signed-out folder prints its answer and
    exits 1, so the adapter reads the output and ignores the exit code. `cli.py` never names the command or a field; the adapter parses, and the parser is
    tested against recorded outputs in `tests/fixtures/<version>/`. A new Claude Code version means
    a new fixture folder, not new test code. Everything is held in memory: `identity` is a hash of
    org id and email, used only to tell whether two folders are one login; the email, org id and
    org name are never printed or stored. How the answer is used, in each of `status` and `run`:
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
    never a traceback); each account's plan exists and its sign-in check answers; the hub's one
    settings file still calls capture; the `fairshare` command it calls exists and runs
    (`--help`; with no hook wired, the installed one). It reads settings and changes nothing, and
    prints account ids and plan names only, so the output can go into a public issue. A repairing
    `--fix` (reusing the start check) is stage 3b.

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

## Known limits

- Below the "nearly full" line, short-window usage plays no part in the ranking.
- A snapshot's age is never read, so a stale snapshot is trusted until its windows reset.
- Idle sessions redraw their statusLine with old values. Within one window (reset times within
  an hour of each other) a lower used % is treated as stale and the stored one kept, so usage
  cannot roll back; an idle redraw of unchanged values still counts as fresh.
- The ranking is greedy; it cannot promise full use of every week.
- The usage hook calls the installed `fairshare` by the absolute path found when `setup` or
  `join` last ran; if it moves (a reinstall elsewhere), `fairshare doctor` fails and running
  `fairshare setup` again rewrites the hook (it rewrites any older capture).
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
- Claude Code's docs say `claude auth status` exits 1 when signed out; the sign-in check treats a
  non-zero exit as no answer (decision 10), so a signed-out account may read as a failed check
  (the recorded fixtures keep the output, not the exit code). Not checked or changed in this
  stage.
- `run -a` takes the account only as its first argument; later `-a` goes to the provider.
- Ctrl-Z and `fg` are tested by sending the stop and continue signals to a supervised fake
  child; a real terminal (interactive shell in a pty) was checked by hand once, 2026-11-09.
- Plan detection has seen only Claude Pro: the `"max"` and `"ultra"` names are unverified; an
  unmatched value leaves the plan in `accounts.toml`.
- Per-model weekly limits (e.g. a Fable week, seen on Pro as well) are not read yet; they will
  come from the provider's usage report for whatever plan reports them, never from plan data.
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
| Sign-in check: `auth status --json` and its fields (`loggedIn`, `authMethod`, `email`, `orgId`, `planTier`) | `STATUS_ARGS` (per account `status_args`), `STATUS_*`, `parse_account_status` | fails soft (decision 10): login file and `accounts.toml` plan | 2.1.322, 2026-11-10 |
| Version: `--version` | `VERSION_ARGS`, `version` | `doctor` prints "version unknown" | 2.1.322, 2026-11-09 (prints `2.1.322 (Claude Code)`) |
| Account folder variable (`CLAUDE_CONFIG_DIR`) | `CONFIG_DIR_ENV`, `clean_env` | none: accounts could not be told apart | 2.1.321, 2026-11-09 (sign-in, sign-out and usage per folder, independently) |
| Login-override and session variables | `LOGIN_OVERRIDE_ENV`, `SESSION_ENV`, `SESSION_MARK_ENV`, `WATCH_ENV_PREFIX`, `passed_session_vars` | a variable missing from the lists reaches the child: add it (inside a session the launcher names unlisted `CLAUDE_*` ones) | session variables 2.1.322, 2026-11-09 (the names a running session injects into its children; inherited, they stop the child saving a transcript; scrubbing fixes it); login-override variables not tested live |
| Usage hook: the `statusLine` entry in `settings.json` and its payload (`rate_limits`, `used_percentage`, `resets_at`) | `SETTINGS_FILE`, `STATUS_LINE_KEY`, `HOOK_*`, `CAPTURE_*`, `wire`, `wired`, `parse_capture` | no usage is recorded; accounts rank as fresh; `doctor` shows the hook | 2.1.321, 2026-11-09 (documented fields; live capture on two accounts) |
| Share list: observed `projects/`, `history.jsonl`, `file-history/`, `paste-cache/`, `plugins/`, `skills/`, `tasks/`, `settings.json`, `sessions/`; documented user-level `CLAUDE.md`, `agents/`, `commands/`, `output-styles/`, `keybindings.json` | `SHARED` | an item missing from the list stays per account; one renamed leaves a stale link (harmless); `NOT_COPIED` (`sessions/`) is shared but never seeded | observed: 2.1.322 (real config dirs); documented: code.claude.com/docs/en/claude-directory, read 2026-11-09 |
| Settings written through a symlinked `settings.json` | `SHARED` (the start check repairs either way) | a replaced link is repaired at the next launch, the difference shown | 2.1.322, 2026-11-09 (throwaway folder, first-run theme picker: link kept, target rewritten) |
| `history.jsonl` written through a link | `SHARED` | as above (lines merged) | not provoked: needs a login |
| Sign-in and sign-out: `auth login`, `auth logout` | `LOGIN_ARGS`, `LOGOUT_ARGS`, `login_command`, `logout_command` | `join` reports the sign-in did not finish; `leave --logout` changes nothing | documented (CLI reference, read 2026-11-09); not run live |
| State file `.claude.json` inside the folder variable's folder (beside `~/.claude` in HOME without it), its lock folder `.claude.json.lock` | `STATE_FILE`, `STATE_LOCK`, `DEFAULT_DIR`, `state_file`, `state_lock` | `join --from` copies no state | location: docs (claude-directory) and 2.1.322 (created in the throwaway folder); lock: brief, not observed |
| State keys `join` copies: `projects.<path>.hasTrustDialogAccepted`, `mcpServers`, `hasCompletedOnboarding`, `lastOnboardingVersion` | `STATE_PROJECTS`, `STATE_TRUST`, `STATE_MCP`, `STATE_ONBOARDING`, `merge_state` | the first-run screens or trust prompts show again | 2.1.322, 2026-11-09: a fresh folder's first-run screen set none of them before sign-in (the theme went to `settings.json`); the binary sets the two onboarding keys together when onboarding finishes. Not observed after a real sign-in |
| Window names in the payload | `plans/*.toml` (`[windows.*] name`) | edit the plan file | `five_hour`, `seven_day`: 2.1.321, 2026-11-09 (docs and live capture) |
| Login file (`.credentials.json`), existence only | `LOGIN_FILE`, `login_present` | only used when the sign-in check fails, and then only for an account last confirmed as a subscription login | 2.1.321, 2026-11-09 (present exactly in signed-in folders) |
| Shell aliases that set the folder variable; the folders to look in | `RC_FILES`, `ACCOUNT_DIR_GLOB`, `discover` | `setup` lists fewer folders; `join --from <folder>` names one | not applicable |
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

OpenAI/Codex adapter · stale-snapshot warning in `status` ·
mid-session handover · packaging for Homebrew.
