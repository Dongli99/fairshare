# Architecture

```
statusLine / provider output ──► providers/<p>.parse_capture ──► store (snapshot per account)
                                                                   │
accounts.toml ──► model.load_accounts ───────────────┐             ▼
plans/*.toml  ──► model.load_plan                    └──► policy.rank(accounts, snapshots, now)
                                                                   │
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
   plan declares a relative capacity (Pro = 1). Override per account in `accounts.toml`.
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

## Known limits

- Below the "nearly full" line, short-window usage plays no part in the ranking.
- A snapshot's age is never read, so a stale snapshot is trusted until its windows reset.
- The ranking is greedy; it cannot promise full use of every week.
- A PATH entry added to the shell rc after fairshare's block can put the real `claude` ahead of
  the shim; re-running `fairshare apply` moves the block back to the end.
- The shim and statusLine call fairshare through the Python that ran `apply`; if that Python
  moves, run `fairshare apply` again.

## Not built yet

OpenAI/Codex adapter · stale-snapshot warning in `status` ·
mid-session handover · packaging for Homebrew.
