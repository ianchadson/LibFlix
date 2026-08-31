# LibFlix Agent Guide

This file is the repository-wide operating guide for coding agents. Read
`README.md` and the applicable source and tests before making changes.

## Project

LibFlix is a Python/Flask application with vanilla JavaScript and CSS. Open
Library work IDs are the canonical book identity. Discovery and download logic
are intentionally separate; preserve that boundary. See `ARCHITECTURE.md` for
data flows, API contracts, caching, and resilience behavior.

Important locations:

- `app.py`: Flask application and most server behavior
- `topic_discovery.py`: topic planning, relevance, and ranking
- `downloaders/`: modular download-source integration
- `templates/` and `static/`: server-rendered UI and browser behavior
- `tests/`: regression suite
- `.github/workflows/`: current CI, benchmark, and deployment behavior

## Cloud environment

Use the universal Linux image and Python 3.14 to match GitHub Actions. No Mac or
local user files are required.

Setup command:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Keep container caching enabled. Keep agent internet access off unless a task
explicitly requires a named public upstream and the user approves the narrow
allowlist. Dependency installation may use setup-phase internet access.

Do not configure repository or production secrets in the agent environment.
LibFlix's normal unit and integration tests require no credentials.

## Working rules

1. Start from current `main` and create a `codex/<short-task-name>` branch.
2. Never push directly to `main`.
3. Make the smallest change that completely addresses the request.
4. Add or update regression tests for behavior changes.
5. Run the relevant focused tests, then the full verification suite.
6. Review `git diff` and `git status` before committing.
7. Push only the feature branch and prepare a pull request for human review.
8. Do not deploy. The `Deploy` workflow runs on pushes to `main` and reaches
   production, so agents must leave merging to the repository owner.

## Verification

Use a disposable data directory so tests cannot read or overwrite repository or
production state:

```bash
LIBFLIX_TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$LIBFLIX_TEST_ROOT"' EXIT

LIBFLIX_DATA_DIR="$LIBFLIX_TEST_ROOT/data" \
LIBFLIX_RATE_LIMITING_ENABLED=0 \
PYTHONPYCACHEPREFIX="$LIBFLIX_TEST_ROOT/pycache" \
PYTHONWARNINGS='error::ResourceWarning' \
unshare --net --map-root-user -- \
  python -m unittest discover -s tests -v

PYTHONPYCACHEPREFIX="$LIBFLIX_TEST_ROOT/pycache" python -m py_compile \
  app.py topic_discovery.py nyt_bestsellers.py book_preparation.py \
  kindle_delivery.py security_runtime.py downloaders/*.py
node --check static/download-ui.js
node --check static/libflix-pwa.js
node --check static/libflix-sw.js
python -m json.tool static/manifest.webmanifest >/dev/null
```

The `unshare` wrapper disables outbound networking at the operating-system
namespace level and is verified in the Codex universal cloud image. Some topic
API tests can schedule best-effort background refreshes after their assertions,
so keep this isolation in place for the aggregate suite. Tests must remain
deterministic without network access; never solve a test failure by adding
credentials or production access. If `unshare` is unavailable in another
environment, use that environment's network sandbox and clearly distinguish a
network-policy block from a test assertion failure.

For UI work, use an isolated headless browser context and the routes listed in
`README.md`. Never use a personal browser profile.

## Public repository safety

This repository is public. Never commit, paste into issues or pull requests, or
include in logs:

- passwords, tokens, API keys, cookies, private keys, or credential files
- `.env` contents or secret environment-variable values
- production host access details or operational credentials
- personal data, local user files, browser profiles, or private conversation
  content
- runtime databases, caches, downloaded books, generated archives, or logs

Use synthetic fixtures and reserved example domains in tests. Before every
commit, inspect staged changes for credentials and unintended generated files.
If a task appears to require a secret, production access, deployment, or a push
to `main`, stop and ask the repository owner instead.

## Pull requests

Pull requests should include:

- a concise problem and solution summary
- the verification commands run and their results
- any known limitations, network requirements, or untested UI behavior
- screenshots only when visual behavior changes, with no personal or sensitive
  information visible

Do not merge pull requests or enable deployment workflows unless the repository
owner explicitly requests that separate action.
