# Security Policy

## Supported version

Security fixes are applied to the `main` branch.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** for this repository
(Security → Report a vulnerability). Do not open a public issue for a suspected
vulnerability.

Include:

- a description of the issue and affected component,
- reproduction steps or a proof of concept,
- the potential impact,
- any suggested mitigation.

Never include real API keys, GitHub tokens, private repository contents or other
secrets in a report. Use redacted placeholders.

## Security model and trust boundaries

CodingAgent is an approval-gated tool, not a hostile-code sandbox.

- The model can only use explicitly defined tools.
- File writes require explicit user approval and are restricted to an isolated
  `git worktree`.
- Commits, pushes and draft PRs are separate, gated actions. The tool has no
  merge or issue-close capability.
- Repository content, issue bodies and comments are treated as untrusted input.
- Test containers run without network access, as a non-root user, with dropped
  capabilities, a read-only root filesystem, and only a sanitized test copy
  mounted.
- API keys are not passed into the test container. Git and `gh` subprocesses do
  not inherit OpenAI credentials.

## Important limitations

- Docker **image builds** install project dependencies and may execute project
  build code with network access. Run the agent only against repositories you
  trust.
- Test code can read the sanitized project copy. Secrets embedded in ordinary
  project files may therefore be visible to the test process.
- The Docker daemon is trusted. The agent does not mount the Docker socket into
  the test container.
- Repository files and issue content read by the model are sent to the OpenAI
  Responses API. `store=False` is used, but this is not a guarantee about
  provider-side retention. Follow your organization's data policy.
- `--max-cost` is a conservative client-side budget guard, not a guaranteed
  billing limit.
- Secret detection is best-effort and may miss unusual formats or produce false
  positives.
- Prompt injection in repository or issue content can affect model suggestions
  or consume API budget, but cannot bypass the code-enforced approval gates,
  tool allowlist, worktree boundary or Docker isolation.
