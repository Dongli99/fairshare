---
name: fairshare
description: Use in a session started by fairshare (which spreads work across several accounts) to start background workers for big or long work, keep `fairshare watch` running while they live, act on fairshare's usage warnings, write and request checkpoints, pick up stopped work, hand off, and run `/fairshare` commands.
---

# Working under fairshare

fairshare runs this session on one of several accounts and says when an account is nearly
spent. The main session (you, unless you were told you are a worker) decides for itself and its
workers. A worker makes no account decisions: it checkpoints when asked.

## Worker or subagent
- Small, short work: a subagent (the Agent tool). fairshare tells subagents nothing; if the
  limit hits, you and the subagent stop together and one pickup covers both.
- Big or long work: a worker, a background session that can be stopped, moved and relaunched:
  `fairshare worker --model <model> --effort <level> "<prompt>"` (optional `--account ID`,
  `--name NAME`). It prints the worker's name and session id.
- Always name a model and an effort fitted to the worker's task: mechanical work (renames,
  field additions, doc edits) gets a cheaper model and a lower effort; design work or changes
  across many files get the strongest model at high effort. For a model without effort levels,
  pass `--effort none`.
- A worker needs the folder trusted on its account. If another account trusts it,
  `fairshare trust` copies that trust (it never creates trust).

## While workers live
- Keep `fairshare watch` running as a background command. It exits when something concerns you
  (your account near its limit, a worker's checkpoint, a worker stopped by its limit, ended or
  idle) and prints one line per event. Act on them, then start `fairshare watch` again.
- `fairshare checkpoint --show <worker name>` prints a worker's note.
- A worker stopped by its limit: watch says when that limit resets, and Claude Code resumes the
  worker by itself then, with its whole context. If that is soon, let it wait; otherwise
  `fairshare pickup <its session id>` continues it on the best account now, in the background.
- To stop a worker, use your own tools (`claude stop`, or a message to it).

## When fairshare warns you
A warning names the account, its percent, when that window resets and the best other account.
- Little work left: carry on.
- Otherwise, at the next stopping point, `fairshare checkpoint --request all` (your workers
  checkpoint), then do the move the warning names; the setting `warning_move` picks it
  (default handoff):
  - **handoff**: `fairshare handoff "<what was done, what is next, open decisions, constraints
    the user set, what was tried and failed, files touched>"`. A fresh session starts on the
    best account and reads that note first; it can read this session's transcript for anything
    the note missed.
  - **pickup**: `fairshare checkpoint "<where you stand>"`, then `fairshare pickup`. This same
    session continues on the best account with its whole context (on another account that
    context is read again once, uncached).

## Checkpoints
- `fairshare checkpoint "<what is done, what is next>"` appends to this session's note.
- `fairshare checkpoint --request <worker name | session id | all>` asks workers for one; each
  hears it at its next step.

## `/fairshare` and `! fairshare`
They are the same verbs with the same arguments. `! fairshare ...` (typed by the user) runs at
once, costs no turn and works when the account is out. `/fairshare ...` comes to you: on
`/fairshare <args>`, run `fairshare <args>` exactly as given, except `handoff` with no note:
write the note yourself and run `fairshare handoff "<note>" <the same flags>`.

Never run fairshare commands for a subagent: they are for the main session and its workers.
