# AGENTS.md

Agent guidance for this repository. See `CLAUDE.md` for setup, architecture,
config, and testing conventions — this file documents the **release workflow**.

## Releasing

Releases are **tag-triggered**: pushing a `vX.Y.Z` tag runs
`.github/workflows/release.yml`, which (1) creates a GitHub Release with
git-cliff notes, (2) builds the sdist + wheel and runs `twine check`, and
(3) publishes to **PyPI** (https://pypi.org/project/telegram-assistant/).

### Cut a release

**Never edit the version by hand.** Use `bump-my-version` — it updates both
version locations, commits, and tags in one step.

```bash
source .venv/bin/activate
bump-my-version bump patch     # or: minor | major   (0.2.1 -> 0.2.2 / 0.3.0 / 1.0.0)
git push origin master --follow-tags
```

`bump-my-version` (config in `[tool.bumpversion]` of `pyproject.toml`):

- bumps `version` in `pyproject.toml` **and** `__version__` in
  `src/telegram_assistant/__init__.py` (kept in sync),
- commits as `chore(release): vX.Y.Z`,
- creates the annotated tag `vX.Y.Z`.

Requirements: clean working tree (`allow_dirty = false`) and the `.venv`
activated so the `git commit` pre-commit hook (git-cliff changelog) is on PATH.

### What the tag triggers

`.github/workflows/release.yml` (on `push` of tags `v*`) has three jobs:

| Job | Does |
| --- | --- |
| `github-release` | git-cliff `--latest` release notes → `softprops/action-gh-release` |
| `build` | `python -m build` (sdist + wheel) → `twine check` → upload `dist/` artifact |
| `pypi-publish` | `pypa/gh-action-pypi-publish` using the `PYPI_API_TOKEN` repo secret |

### Versioning

SemVer. Pre-1.0 (`0.x`), a **breaking change is a minor bump** (e.g. the
Planfix-plugin extraction shipped as `0.2.0`); bugfix-only is a patch.

### PyPI auth

Publishing uses an **API token**, stored as the GitHub Actions repo secret
`PYPI_API_TOKEN` (the publish action defaults the username to `__token__`).
To rotate: create a token scoped to the `telegram-assistant` project at
https://pypi.org/manage/account/token/ and `gh secret set PYPI_API_TOKEN`.
Switching to PyPI Trusted Publishing (OIDC, no stored token) is possible —
it needs a trusted publisher registered on PyPI plus `id-token: write` + a
`pypi` environment on the `pypi-publish` job.

### Verifying a release

```bash
curl -s https://pypi.org/pypi/telegram-assistant/json | python3 -c "import sys,json;print(json.load(sys.stdin)['info']['version'])"
gh run watch "$(gh run list --workflow=Release --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
```
