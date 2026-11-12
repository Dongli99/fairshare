---
name: fairshare
description: Use in a session started by fairshare (which spreads work across several accounts) to start background workers for big or long work, keep `fairshare watch` running while they live, act on fairshare's usage warnings, write and request checkpoints, and pick up stopped work.
---

# Working under fairshare

fairshare runs this session on one of several accounts and says when an account is nearly
spent. The main session (you, unless you were told you are a worker) decides for itself and its
workers. A worker makes no account decisions: it checkpoints when asked.

## Worker or subagent
- Small, short work: a subagent (the Agent tool). fairshare tells subagents nothing; if the
  limit hits, you and the subagent stop together and one pickup covers both.
- Big or long work: a worker, a background session that can be stopped, moved and relaunched:
  `fairshare worker "<prompt>"` (optional `--account ID`, `--name NAME`). It prints the
  worker's name and session id.
- A worker needs the folder trusted on its account. If another account trusts it,
  `fairshare trust` copies that trust (it never creates trust).

## While workers live
- Keep `fairshare watch` running as a background command. It exits when something concerns you
  (your account near its limit, a worker's checkpoint, a worker stopped by its limit, ended or
  idle) and prints one line per event. Act on them, then start `fairshare watch` again.
- `fairshare checkpoint --show <worker name>` prints a worker's note.
- A worker stopped by its limit: `fairshare pickup <its session id>` continues it on the best
  account, in the background.
- To stop a worker, use your own tools (`claude stop`, or a message to it).

## When fairshare warns you
A warning names the account, its percent, when that window resets and the best other account.
- Little work left: carry on.
- Otherwise, at the next stopping point: `fairshare checkpoint --request all` (your workers
  checkpoint), `fairshare checkpoint "<where you stand>"`, then `fairshare pickup`: a new
  session continues on the best account and reads your note first.

## Checkpoints
- `fairshare checkpoint "<what is done, what is next>"` appends to this session's note.
- `fairshare checkpoint --request <worker name | session id | all>` asks workers for one; each
  hears it at its next step.

Never run fairshare commands for a subagent: they are for the main session and its workers.
