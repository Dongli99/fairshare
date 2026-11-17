# fairshare

Routes AI-assistant sessions across several subscriptions by real usage. Open-source (MIT), currently private. **Nothing in this repo may reference any other project, private host, path, account or person's credentials.** One exception: the README's "Prior art" credit.

## Layout
`src/fairshare/policy.py` the rule (pure) · `model.py` data + config loading · `providers/` one adapter per provider · `plans/` data files · `store.py` snapshots · `hub.py` the hub: links, start check, seeding · `browser.py` per-account sign-in browser · `session.py` runs the child session (signals, exit code) · `cli.py`. Seams and decisions: `docs/architecture.md`. Stance on terms: `docs/terms-and-limits.md`.

## Run
`PYTHONPATH=src python3 -m unittest discover -s tests -v` (stdlib only: no pytest, no dependencies, ever, without a reason written in docs/architecture.md).

## Rules
1. Restraint: add a field, flag or parameter only when no change to what exists does the job. A fix leaves the code simpler.
2. The policy stays pure. I/O goes in `store.py`, `hub.py`, `browser.py`, `providers/` and `cli.py`; `model.py` reads the config and plan files.
3. Never read, copy or log a login, token or cookie. Never call an undocumented endpoint. Never edit a user's file without showing the diff and getting a yes for that file; back it up first, and keep the edit reversible by `fairshare uninstall`.
4. A new provider or plan must need no change to `policy.py`. If it does, the abstraction is wrong: stop and say so.
5. A wrong ranking is cheap and a lost or wrong *record* is not: snapshots are written atomically and never overwritten with less information.
6. Commit after each validated change, and only when asked. Before committing run `git diff --staged --stat`: every file must belong to the task. Build or tests fail twice: stop and report.
7. Quote real test output when reporting. A report is a claim until checked against the repo.

## Delegating
- Inline: a change under ~50 lines in one file, or any change to `policy.py`.
- An implementer session: work spanning several files, or an investigation that needs a lot of code in view.
- A cheap-model subagent: mechanical, fully scoped work only (renames, field additions, doc edits).
- Name the model and the effort on every delegation. Never inherit a default.
- Subagent rules, to include in every brief: do only the stated task; match adjacent style; do not commit; if a step is ambiguous, stop and report.
