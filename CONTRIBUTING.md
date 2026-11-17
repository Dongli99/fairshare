# Contributing

Thanks for looking. fairshare is small on purpose; these rules keep it that way.

## Run the tests

```
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Python 3.11+, the standard library only: no pytest and no runtime dependencies. A new
dependency needs a reason written in `docs/architecture.md` first.

## Before you write code

- Read `docs/architecture.md` (the seams and the numbered decisions) and
  `docs/terms-and-limits.md` (what fairshare will never do).
- Open an issue first for anything larger than a fix, so we can agree on the shape.

## Rules a change must keep

1. **Restraint.** Add a field, flag or option only when no change to what exists does the job.
   A fix should leave the code simpler.
2. **The policy stays pure.** `policy.py` does no I/O. A new provider or plan must need no
   change to it; if it does, the abstraction is wrong, and that is worth an issue.
3. **No credentials.** Never read, copy, store or log a login, token or cookie. Never call an
   undocumented endpoint.
4. **The user's files.** Never edit a file outside the hub without showing the diff and getting
   a yes for that file; back it up first.
5. **Records are precious.** A wrong ranking is cheap; a lost or wrong record is not. Writes are
   atomic and never replace a record with one that holds less.
6. **Vendor facts are constants.** Anything a provider may rename (a command, a flag, a field)
   is one named constant in that provider's file, with a recorded fixture in `tests/fixtures/`.
   Scrub fixtures of every `*_id` value and anything personal.

## Pull requests

- One change per pull request, with tests. Quote the test run in the description.
- Keep to the style of the code around it: short functions, plain names, docstrings that say
  what and why.
- fairshare is not affiliated with any provider. Do not add anything that implies endorsement,
  and keep provider trademarks out of names.
