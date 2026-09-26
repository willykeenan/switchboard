# Contributing

Switchboard is a local-first Python 3.9+ project. Keep the stdlib-only runtime unless a dependency is discussed in the issue.

## Setup

```sh
python3 -m switchboard --install
python3 -m unittest discover -s tests
```

## Rules

- Loopback HTTP only. Do not add bind-all defaults.
- Keep every write inside `store.data_root()`; resolve room paths with `store.rooms_root()`.
- Nothing may start a provider process unless `store.launch_allowed()` is true.
- Do not commit `~/.switchboard`, control tokens, or real session transcripts.
- Invented fixtures only. No personal paths, emails, or live Codex thread IDs.
- Prefer editing an existing module over a parallel framework.
- Tests for removed proprietary runners stay deleted.

## Pull requests

Include the command you ran and what you verified. CI runs the three test commands in `.check` and a `pip install .` smoke test.
