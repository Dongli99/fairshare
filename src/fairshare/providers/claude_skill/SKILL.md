---
name: fairshare
description: Use in a session started by fairshare (which spreads work across several accounts) to start background workers for big or long work, keep `fairshare watch` running while they live, write and request checkpoints, act on a line about the handoff stage, and run `/fairshare` commands.
---

# Working under fairshare
fairshare picks the account each session starts on. A main session (you, unless told you are a
worker) hears from it only about the handoff stage. A worker hears only its main session.

## Worker or subagent
- Small, short work: a subagent (the Agent tool). Never run fairshare commands for a subagent.
- Big or long work: a worker, a background session fairshare can stop and move:
  `fairshare worker --name <name> --model <model> --effort <level> "<prompt>"` (`--account ID`).
- `--name`: the task in a few words, lowercase, hyphenated, after a short project prefix
  (`fs-watch-stops`); names are unique across all workers, so a retry adds `-2`. Never an id.
- Model and effort fit the task: mechanical work (renames, field additions, doc edits) a cheaper
  model at lower effort; design or many-file changes the strongest at high; `--effort none` for
  a model without effort levels. `fairshare trust` copies folder trust another account has.

## While workers live
- Keep `fairshare watch` running as a background command. It exits with one line per event (the
  handoff stage, a worker's checkpoint, limit, turn end, wait for an answer or end); act on it,
  then start it again. A worker at its turn end waits for you: `fairshare transcript` shows it.
  One waiting for an answer needs a person: tell the user at once, with the `attach` command.
- A worker stopped by its limit resumes by itself at the reset; `fairshare pickup <its session
  id>` moves it now (it stops only your own workers: stop any other session first, `fairshare
  run -a <its account> stop <its session id>`). Done with a worker: `fairshare worker --remove`.
- `fairshare checkpoint "<what is done, what is next>"` notes where you stand; `--request <worker
  name | session id | all>` asks workers for one; `--show <worker name>` prints one.

## The handoff stage: do what the line says
- Hand off: run the `fairshare handoff "<note>"` it gives (done, next, open decisions, files; a
  project's own handoff form wins); it moves your workers here and continues in a fresh session.
- No account can take the work: write your checkpoint, then keep working into the limit.
- A worker's account: run the `fairshare pickup` it names. "Over": nothing to do; keep working.

## `/fairshare` and `! fairshare`
Same verbs and arguments. `! fairshare ...` (the user's) runs at once, costs no turn, works when
the account is out. `/fairshare <args>` comes to you: run `fairshare <args>` as given, except
`handoff` with no note: write the note yourself, then `fairshare handoff "<note>" <same flags>`.
