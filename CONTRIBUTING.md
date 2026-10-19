# Contributing

## Changes

Work on a branch and open a pull request into `main`. Explain the problem, the
change and how you verified it.

Set up the virtual environment and hooks as in the README's local development
steps, then let the hooks run on every commit and push:

- **pre-commit:** the secret scans, Ruff lint and format, and Mypy.
- **pre-push:** the same, plus the full Pytest suite, a pip-audit of both locks
  and the changelog check.

Every one has to pass; don't push with `--no-verify`. CI runs the same checks
again on the pull request.

New logic comes with tests in the same pull request. Every commit that changes
`src/` needs a `CHANGELOG.md` entry under `Unreleased`, in it or in a later
commit on the branch; one with no effect anyone running the bot would notice
opts out with a `Changelog: none` trailer.

## Commit messages

Lowercase and imperative, saying what changed against what it replaced, for
example `skip a track that won't stream, instead of dropping to radio`.

## Dependency updates

One package per pull request. Change only `requirements*.in` and regenerate the
matching lock with the `pip-compile --generate-hashes` command in the README.
Read the upstream release notes and the code diff between the published
versions, and look for new install hooks, dependencies, or network,
environment or file access before merging. Passing checks alone don't make an
update safe.
