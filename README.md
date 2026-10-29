# CodingAgent

A small, approval-gated coding agent for Git and GitHub repositories. The agent
uses the OpenAI Responses API with your own API key and is therefore independent
of Codex subscription usage limits.

This version is an evolution of the original `agent.py` and keeps a simple
terminal interface. The most important principle is:

> The model may propose and write code, but it cannot bypass approvals, leave
> the worktree, commit to a protected branch, push, or create/merge pull
> requests on its own.

## Features

- Automatic branch and isolated `git worktree` for every task.
- GitHub Issues via `gh` (description, comments, labels, acceptance criteria,
  dependencies on issues/PRs).
- A plan is presented and requires approval before any file is changed.
- Every file write is shown as a diff and requires approval.
- Tests run in a separate Docker container: non-root, no network, resource
  limited, and without the host home directory, Docker socket, SSH keys or API
  keys.
- Automatic commit only after tests pass.
- Push and draft PR require explicit approval. There is no automatic merge and
  no automatic issue closing.
- Per-task cost control with `--max-cost`, token accounting, cached tokens, a
  local JSONL log and a configurable, versioned pricing file.
- `--dry-run` for analysis and planning without branches, worktrees, file
  changes, commits, cost logs or PRs.

## Requirements

- macOS (Linux largely works too).
- Python 3.11 or later.
- Git.
- Docker Desktop (or Docker Engine) with a running daemon.
- GitHub CLI, `gh`.
- An OpenAI API key with access to the Responses API.

The agent does **not** use the Codex CLI or the Codex subscription. API calls
are billed separately according to OpenAI's API prices.

## macOS installation

### 1. System tools

```bash
brew install python@3.12 git gh
brew install --cask docker
open -a Docker
```

Wait until Docker Desktop has started, then verify:

```bash
docker info
gh --version
git --version
```

### 2. Python environment

```bash
cd ~/ai-coding-agent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

`requirements-dev.txt` also includes pytest. If you want to start the agent as
a command, you can additionally run:

```bash
pip install -e .
ai-coding-agent --help
```

Otherwise, the simplest approach is to always run `python agent.py ...` from
`~/ai-coding-agent` with the virtual environment activated.

### 3. GitHub CLI and repository

```bash
gh auth login
gh auth status
git -C /path/to/ProjectA remote -v
```

There must be a remote named `origin`, and `gh` must be authenticated against
the GitHub instance that hosts the repository. The agent reuses your existing
`gh auth` and never asks for a GitHub token in the terminal.

Issue-based work requires the repository to have a GitHub origin. The agent
refuses to work against a local-only directory.

### 4. OpenAI API

```bash
export OPENAI_API_KEY="sk-..."
# Optional:
export OPENAI_MODEL="gpt-5.4"
```

Never put the key in the repository. The agent reads it from the environment,
does not pass it to the Docker container, and does not write it to logs. It does
not load `.env` automatically.

**Data sharing:** files, issue descriptions and comments that the model reads
are sent to the OpenAI Responses API as part of the prompt. The agent uses
`store=False`, but that is not a guarantee of how the provider handles data
after the request. Follow your organization's policy before running the agent
against private or sensitive code.

Verify:

```bash
python agent.py --version
python agent.py --help
```

## Quick start

### Analysis mode without side effects

```bash
cd ~/ai-coding-agent
source .venv/bin/activate
python agent.py ~/Projects/ProjectA --issue 963 --dry-run
```

In a dry run:

- the repository and GitHub issue are read,
- Docker is not required,
- no branch or worktree is created,
- no files, commits or PRs are created,
- no cost log is written (cost is shown only in the terminal),
- no approval is requested.

### Issue-based development: ProjectA

```bash
python agent.py ~/Projects/ProjectA \
  --issue 963 \
  --model gpt-5.4 \
  --max-cost 2.00 \
  --reasoning-effort medium
```

### Issue-based development: ProjectB

```bash
python agent.py ~/Projects/ProjectB \
  --issue 123 \
  --model gpt-5.4 \
  --max-cost 2.00
```

`ProjectA` and `ProjectB` are neutral examples. Replace them and the example
paths with your actual repositories. Specify `--base-branch` if the
repository's default branch cannot be detected automatically.

### Free-form task without an issue

```bash
python agent.py ~/Projects/ProjectB \
  "Add validation for empty organization numbers and update the tests" \
  --max-cost 1.50
```

## Workflow and approvals

1. **Preflight**: the Git repository, Docker, GitHub CLI/authentication and
   OpenAI configuration are verified. The model price must be known.
2. **Issue analysis**: the issue, comments, labels, acceptance criteria and
   referenced issues/PRs are fetched. The content is treated as untrusted input.
3. **Plan**: the agent examines code and tests and presents a concrete plan.
4. **Plan approval**: no files are changed until you answer `y`.
5. **Isolated worktree**: a new branch is created, for example
   `agent/issue-963-fix-login`, in a worktree outside your regular working copy.
6. **Code changes**: every `write_file` displays a diff and requires a separate
   approval.
7. **Tests**: relevant tests can be run during the work. When the work is
   finished, the agent always runs the full test suite in Docker.
8. **Results**: test results, file status and diff statistics are shown. If tests
   fail, **no commit** is made and the changes remain in the worktree.
9. **Commit**: if tests pass, an automatic commit is created with
   `Refs #<issue>` (never `Fixes`/`Closes`) and a summary.
10. **Push/PR**: the agent shows a proposed draft PR (title and description) and
    asks for explicit approval. Only then is the branch pushed and a **draft**
    PR created.
11. **Final report**: branch, commit, worktree, tests, cost and any PR link are
    displayed.

The agent never runs `gh pr merge`, `gh issue close` or equivalent commands. A
merge must be performed manually by a human.

## Git, branches and worktrees

- Branch format: `agent/issue-<number>-<short-slug>`, for example
  `agent/issue-963-fix-login`.
- If the name already exists, a unique suffix is added (`-2`, `-3`, ...).
- Worktrees are created by default under:
  `~/.local/state/ai-coding-agent/worktrees/<repo>-<hash>/<branch>`.
- Use `--worktree-root` to choose a different directory.
- Your regular working copy is never checked out to the agent branch, and its
  branch/HEAD is not changed. This is verified before and after the work.
- The agent refuses to write or commit on `main`, `master`, the base branch, or
  any other branch that does not start with `agent/`.
- Only files the agent actually wrote are staged. Filenames such as `.env`,
  `.env.*` (except clear templates such as `.env.example`), `*.pem`, `*.key`,
  credentials files, SSH keys and similar are blocked.
- Staged changes are scanned for high-confidence secret patterns. On a match,
  the commit is aborted and the index is reset.
- Git hooks and GPG signing are disabled for the agent's automatic commit
  (`--no-verify`, `commit.gpgsign=false`) so that repository code is not
  executed as part of the commit process.

If your regular working copy has uncommitted changes, the agent warns you. The
changes are not copied into the worktree, which is based on the HEAD of the base
branch. It is best to commit or stash before starting so that the plan is based
on exactly the same code as the worktree.

Worktrees are not removed automatically because they may be needed for review
or recovery. Manual cleanup:

```bash
git -C /path/to/repo worktree list
git -C /path/to/repo worktree remove /path/to/worktree
git -C /path/to/repo branch -D agent/issue-963-fix-login
```

## GitHub Issues

For `--issue`, the agent fetches:

- number, title, body, state and URL,
- labels,
- comments,
- a best-effort extraction of acceptance criteria from headings and checkboxes,
- references to other issues/PRs (`#123` and full GitHub URLs) and their
  title/state.

Extracted acceptance criteria are a reading aid, not a contract. Issue text and
comments are untrusted input and must not control the agent's permissions.

When the work is finished, the agent proposes a PR title and PR description.
After your approval, a **draft PR** is created with `gh pr create --draft`. The PR
text is stripped of closing keywords such as `Closes #123` and uses
`Refs #123` instead.

If `gh` or authentication is missing, you receive installation instructions. The
agent does not try to create its own token or bypass `gh auth`.

## Docker-isolated tests

For each test run:

1. A sanitized, writable copy of the worktree is created in a temporary
   directory. `.git`, all `.env*` files, credentials, keys, `.venv`,
   `node_modules`, `dist`, `build` and similar are not copied. Symbolic links are
   skipped.
2. A minimal image is built from `python:3.12-slim` with pytest. The project's
   `requirements.txt`, `requirements-dev.txt` and/or `pyproject.toml` /
   `setup.py` are installed during the image build.
3. The test container runs with:

   - `--network none`
   - `--user <host's non-root UID>` (or `10001:10001` if the agent runs as root)
   - `--read-only` with a writable worktree mount and tmpfs for `/tmp`
   - `--cap-drop ALL`
   - `--security-opt no-new-privileges`
   - `--cpus`, `--memory`, `--memory-swap`, `--pids-limit` and a timeout
   - only the sanitized test copy mounted at `/workspace`
   - no `-e` variables containing API keys, no home directory and no Docker
     socket

4. stdout, stderr and the exit code are collected. Passed/failed tests and a
   summary are displayed.
5. The container, image and temporary test directory are cleaned up in
   `finally`.

Default limits:

| Resource | Default |
| --- | --- |
| CPU | 1.0 |
| RAM | 512 MB |
| Processes | 128 |
| Test timeout | 300 s |
| Image build timeout | 600 s |

Change them with `--test-cpus`, `--test-memory`, `--test-pids` and
`--test-timeout`.

If Docker is missing or the daemon does not respond, the agent aborts with clear
installation instructions. It **never** falls back to running tests directly on
the host.

### Important limitation: the image build

The test run itself has no network. The image build, however, must be able to
reach package indexes to install pytest and the project's dependencies. `pip
install` may execute the project's build code (for example `setup.py` or a build
backend) inside the build container. The build receives no secret environment
variables, no home directory and no Docker socket, but it does have network
access. Therefore, run the agent only against repositories you trust. If you want
to avoid dependency installation, use a project that does not require it, or
customize `PytestProfile` in `coding_agent/docker_tests.py`.

The test profile is extensible. `DockerTestRunner` accepts a `TestProfile`; new
profiles for React/TypeScript can be registered without changing the Git,
GitHub or cost modules. Only `pytest` is registered in this version.

## Cost control

```bash
python agent.py ~/Projects/ProjectA \
  --issue 963 \
  --model gpt-5.4 \
  --max-cost 2.00 \
  --reasoning-effort medium
```

- Default limit: **2.00 USD** per task.
- `--max-cost` must be a positive decimal number.
- `--model` selects the model. The default is `OPENAI_MODEL` or `gpt-5.4`.
- `--reasoning-effort` is sent only if the model's pricing entry declares
  support for it. Otherwise, the run aborts before the first API call.
- Before every API call, a conservative worst-case estimate is made: estimated
  input tokens (25% margin plus request overhead) and the maximum number of
  output tokens. If the estimate would exceed the remaining budget, no call is
  made.
- After every call, input, cached and output tokens are shown along with the
  cost and cumulative total.
- Cached input tokens are treated as a subset of input tokens and priced at the
  cached price.
- Long contexts may have a separate price tier (for example, gpt-5.4 above
  272,000 input tokens). The pricing file includes such tiers where known.
- If the API does not return usage, the conservative pre-call estimate is used
  and a warning is displayed.

### Pricing and versioning

Prices live in `coding_agent/pricing.json`, not hardcoded in the Python code.
The file contains:

- `version`: `2026-10-29`
- `source`: OpenAI's official pricing page
- `currency`: USD
- `unit`: per 1M tokens
- per-model prices and any long-context and reasoning settings

Unknown models produce a clear error and are **never** assumed to cost zero.
Update the pricing file manually when OpenAI changes prices, and bump `version`
and `checked_at`. You can also point to your own file:

```bash
python agent.py ... --pricing-file /path/to/pricing.json
```

### Cost log

By default, a JSONL log is written per task under:

```text
~/.local/state/ai-coding-agent/costs/
```

The log contains start/end records, model, pricing version, pre-call estimate and
actual usage cost. It does **not** contain prompt text, file contents or API
keys. The file is created with permissions `0600`; new directories are created
with `0700`. `--cost-log-dir` changes the location, but it must be outside the
target repository so that the agent does not write into your regular working
copy. A dry run writes no cost log.

### Estimated cost is not a guaranteed billing limit

The cost is calculated from the token usage reported by the API and the local
pricing file. It may differ from OpenAI's actual invoice because of price
changes, long-context/priority tiers, caching behavior, reasoning tokens,
currency/fees or other terms. The cost of an in-flight API request is not known
in advance. `--max-cost` is therefore a **conservative client-side guard**, not
a guaranteed billing limit. If a request still goes over the limit, all
subsequent requests are stopped.

## CLI reference

| Flag | Description |
| --- | --- |
| `repo` | Path to the Git repository |
| `task ...` | Free-form task (optional when `--issue` is supplied) |
| `--issue N` | GitHub issue to work on |
| `--dry-run` | Analysis/plan only, no side effects |
| `--model` | OpenAI model (default `gpt-5.4` or `OPENAI_MODEL`) |
| `--max-cost USD` | Cost limit, default 2.00 |
| `--reasoning-effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |
| `--pricing-file` | Custom versioned pricing file |
| `--base-branch` | Base branch for the agent branch |
| `--worktree-root` | Directory for worktrees |
| `--cost-log-dir` | Directory for cost logs |
| `--test-timeout` | Test timeout in seconds (default 300) |
| `--test-cpus` | CPU limit (default 1.0) |
| `--test-memory` | Memory limit (default 512m) |
| `--test-pids` | Process limit (default 128) |
| `--test-profile` | Test profile, currently `pytest` |
| `--version` | Version |

There are deliberately no flags to automatically approve a plan, file write,
push or PR. Approvals happen interactively.

## Security and known limitations

### Safeguards

- Only explicitly defined tools: `list_files`, `read_file`, `search_code`,
  `git_status`, `write_file`, `run_tests`, `submit_summary`. There is no general
  shell tool.
- All external commands run with argument lists, never `shell=True`.
- Paths are normalized and must remain inside the worktree. Path traversal,
  symlink escapes and sensitive filenames are blocked.
- File writes require approval and may only happen on `agent/` branches.
- Repository, issue and comment content is treated as untrusted input in the
  system prompt. Model-generated text cannot approve itself.
- Terminal output is sanitized for ANSI/control characters and known secrets.
- Git and gh subprocesses do not receive the OpenAI key in their environment.
  Docker receives no secret environment variables at all.
- Git hooks and GPG signing are disabled for the agent's commit.
- Push requires approval; PRs are created as drafts; merge/close functionality
  does not exist in the codebase.
- No automatic commits on test failure or interrupted runs.

### Limitations you need to know

- **The Docker image build has network access** and installs project
  dependencies. This is necessary to run tests, but it means the project's build
  code runs in the build container. Use trusted repositories.
- **Test code can read the entire sanitized project copy.** It cannot see the
  host home directory, Docker socket, SSH keys or environment variables
  containing API keys, but it may attempt to exfiltrate the project's own source
  code if it has network access. The test run uses `--network none`, but the
  image build has network access. This also assumes secrets have not been
  committed in ordinary project files; the agent blocks known filenames and
  patterns but cannot claim to find every possible secret.
- **The Docker daemon is trusted.** The agent does not mount the socket, but a
  user who can run Docker on the host still has broad powers.
- **Prompt injection can influence the model's suggestions** and waste API
  budget, but cannot bypass the hard approvals, tool allowlist, worktree
  boundary, Docker isolation or push/PR gates.
- **The cost limit is not a billing guarantee** (see above). An in-flight request
  may cost more than the estimate.
- **The model can make incorrect changes.** Review the diff and tests before
  pushing or merging.
- **The Docker build cache may retain layers** from the temporary test copy even
  after the image tag is removed. Clean up if needed with `docker builder prune`;
  this also affects other Docker builds on the machine.
- **Worktrees remain after the run** and may need manual cleanup.
- **Python 3.12 + pytest** is the only completed test profile. React/TypeScript
  requires a future `TestProfile`.
- **Repositories with only `pyproject.toml`** may need a working build backend
  and the Python version used in the test image. Missing system libraries produce
  a clear Docker build error; add them to the project environment or customize
  the profile.
- **The base branch may be older than `origin`.** The agent does not run an
  automatic `git fetch`; run it yourself if you want to start from the latest
  remote commit.
- **No automatic resume.** After an interruption, the worktree and branch are
  shown; you can continue manually or run a new task.

## Troubleshooting

### `Not a valid Git working tree`

Check that the path exists and is a Git repository:

```bash
git -C /path/to/repo rev-parse --show-toplevel
```

### `GitHub CLI (gh) is not installed`

```bash
brew install gh
gh auth login
gh auth status
```

### `GitHub CLI is not authenticated`

```bash
gh auth login
```

The agent uses your existing `gh` authentication and stores no tokens itself.

### `Docker was not found` or `daemon is not available`

```bash
brew install --cask docker
open -a Docker
docker info
```

The agent never falls back to running tests on the host.

### Docker image build failed

Common causes:

- the project's dependencies require system packages that are not present in
  `python:3.12-slim`,
- `pyproject.toml` requires a different Python version or build backend,
- the network cannot reach the package index.

The build error is shown in the terminal. The changes remain in the worktree and
no commit is made.

### `No tests collected` / pytest exit code 5

The agent requires at least one passing test to commit automatically. Add tests
or use another test profile when one is available.

### `No pricing is configured for model ...`

Add the model to `coding_agent/pricing.json` with version, source and price, or
use a model that is already in the pricing file. Unknown prices are never
assumed to be zero.

### `Cost limit reached` / `would exceed the remaining budget`

Increase `--max-cost` or use a cheaper model. The message is a conservative
guard, not a billing guarantee.

### `Git author identity is not configured`

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

### `uncommitted changes` in the source working copy

The agent warns and the worktree is based on HEAD, not the uncommitted changes.
Commit or stash before running for the best results.

### `ModuleNotFoundError: coding_agent` when running pytest

Run from the repository root with:

```bash
python -m pytest -q
```

### Push/PR fails

Check:

```bash
git -C /path/to/repo remote -v
gh auth status
git -C /path/to/worktree status
```

The commit and worktree remain even if push or PR creation fails. The agent does
not merge or close issues.

## Tests

Unit tests mock OpenAI and GitHub calls. There is also a local Git integration
test and a real Docker test that verifies the container runs as non-root, has no
network, does not receive `OPENAI_API_KEY`, and cannot write to the original
repository. Command arguments are also verified to ensure no home directory,
Docker socket or secret environment variable is mounted.

```bash
source .venv/bin/activate
python -m pytest -q
python -m pytest -q -m docker
```

The Docker test is skipped automatically if Docker is not available. No real
OpenAI calls are made in the test suite; a real API run requires your
`OPENAI_API_KEY`.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for module layout, data flow, security
invariants and test profile extensions.

## License

MIT, see [LICENSE](LICENSE).

## Security reporting

See [SECURITY.md](SECURITY.md). Report security issues through GitHub's private
vulnerability reporting, not as a public issue.

## Manual installation steps

The agent does not install system tools or create authentication for you.
Therefore:

1. Install Git, Python 3.12, Docker Desktop and `gh` as described above.
2. Start Docker Desktop and verify with `docker info`.
3. Run `gh auth login` and make sure the repository has an `origin`.
4. Set `OPENAI_API_KEY` in the current shell.
5. Run `pip install -r requirements-dev.txt`.
6. Start with `--dry-run` to review the plan without side effects.
