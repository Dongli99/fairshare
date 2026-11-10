# Architecture

```
statusLine / provider output ──► providers/<p>.parse_capture ──► store (snapshot per account)
                                                                   │
accounts.toml ──► model.load_accounts ───────────────┐             ▼
plans/*.toml  ──► model.load_plan                    └──► policy.rank(accounts, snapshots, now)
                                                                   ▲   │
providers/<p>.account_status (once per command, in parallel) ──────┘   │
   signed in? subscription? same login as another folder? plan?        ▼
                                                  cli: status | pick | run ◄──┘
                                                       run ──► providers/<p>.launch
```

## Seams

| Seam | What varies | Where |
|---|---|---|
| Provider | how usage is read, how the tool starts, where accounts already live | `providers/` (one file each) |
| Plan | window names and lengths, relative capacity | `plans/*.toml` (data only) |
| Policy | the routing rule | `policy.py` (pure function) |
| Storage | where snapshots live | `store.py` |

## Decisions

1. **Provider-neutral windows.** The model has a *short* and a *long* window per plan; a
   plan file names the provider's own window for each role. A provider with daily/monthly
   windows fits without touching the policy.
2. **Capacity is data.** Percentages from different plan sizes are not comparable, so each
   plan declares a relative capacity (Pro = 1). Override per account in `accounts.toml`. The
   Claude values are Anthropic's published weekly multiples (Max 5x, Ultra 10x; see the plan
   files); Ultra has no five-hour window, and a missing window counts as unused.
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

8. **A shim, not a replacement.** `fairshare shim install` writes a small `claude` script into
   its own directory, placed earlier on PATH; the real binary is never touched. With
   `CLAUDE_CONFIG_DIR` already set (an account alias) the shim runs the real `claude` as is;
   otherwise it calls `fairshare run`. Both find the real binary the same way: the first
   `claude` on PATH whose first bytes do not carry the shim marker. Skipping by marker rather
   than by directory name survives PATH spelling variants and a second shim copy, so the shim
   cannot re-enter itself.

9. **Setup edits, shown and reversible.** `wire()`/`unwire()` in the Claude provider are pure:
   current file texts in, new texts out. The CLI shows each change as a diff, asks per file,
   and backs the file up before writing. Reversal needs no state file: a wrapped statusLine
   carries the user's command after `--then`, the rc block is marker-delimited, and a disabled
   alias keeps its text behind a `# fairshare-disabled: ` prefix. An existing statusLine is
   never replaced, only wrapped. Any plain fairshare capture counts as fairshare's to remove.

10. **Ask the provider who is signed in.** `Provider.account_status(account)` returns a neutral
    `AccountStatus` (`logged_in`, `usable`, `identity`, `plan`) or `None` when the question could
    not be answered. For Claude Code it runs the installed binary as `claude auth status --json`
    with the account's `CLAUDE_CONFIG_DIR`, at most 5 s, in an environment scrubbed of every
    variable that could pick another login or carry another session's state
    (`claude.clean_env`, which a launcher reuses). Verified on Claude Code 2.1.321 (2026-11-09):
    the output is a JSON object with `loggedIn`, `authMethod`, `email`, `orgId`, `planTier` among
    others. `cli.py` never names the command or a field; the adapter parses, and the parser is
    tested against recorded outputs in `tests/fixtures/<version>/`. A new Claude Code version means
    a new fixture folder, not new test code. Everything is held in memory: `identity` is a hash of
    org id and email, used only to tell whether two folders are one login; the email, org id and
    org name are never printed or stored. How the answer is used, in each of `status`, `pick`
    and `run`:
    - not signed in, or not a subscription login (`authMethod` other than `claude.ai`): left out
      of the ranking; `status` says why, `run` prints one line;
    - several folders with one identity are one quota: ranked once, by the member with the
      newest snapshot; `status` shows the others as "same login as <id>";
    - the detected plan beats `accounts.toml` for ranking and `init`'s proposal; `status` marks a
      mismatch. Detection reads the plan files' `names` list (`"pro"`, `"max"`, `"ultra"`; only
      `"pro"` has been observed so far), never a table in code;
    - when the check fails (non-zero exit, timeout, bad JSON, no binary) it fails soft: the
      account counts as signed in when its login file exists (existence only, never opened) and
      as signed out when not, takes its plan from `accounts.toml`, and is not grouped. `status`
      names the failure and the fallback; `run` prints one line. A failed check alone never
      stops a launch.

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
    ranking. Ctrl-C exits 130 without launching. `pick` keeps its contract (exit 75).

## Known limits

- Below the "nearly full" line, short-window usage plays no part in the ranking.
- A snapshot's age is never read, so a stale snapshot is trusted until its windows reset.
- Idle sessions redraw their statusLine with old values. Within one window (reset times within
  an hour of each other) a lower used % is treated as stale and the stored one kept, so usage
  cannot roll back; an idle redraw of unchanged values still counts as fresh.
- The ranking is greedy; it cannot promise full use of every week.
- A PATH entry added to the shell rc after fairshare's block can put the real `claude` ahead of
  the shim; re-running `fairshare apply` moves the block back to the end.
- The shim and statusLine call fairshare through the Python that ran `apply`; if that Python
  moves, run `fairshare apply` again.
- Plan detection has seen only Claude Pro: the `"max"` and `"ultra"` names are unverified; an
  unmatched value leaves the plan in `accounts.toml`.
- Max and Ultra also have a separate weekly limit for one model (Fable), which the ranking does
  not read yet.
- The sign-in check relies on `claude auth status --json` (Claude Code 2.1.321). If a release
  changes it, the check fails soft (see decision 10) until a fixture and the parser follow.
- `run` waits by sleeping; it does not watch for a snapshot a running session writes meanwhile
  until it wakes and re-ranks.

## Not built yet

OpenAI/Codex adapter · stale-snapshot warning in `status` ·
mid-session handover · packaging for Homebrew.
