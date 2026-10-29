"""Command-line interface and approval flow."""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Sequence

from openai import OpenAI

from . import __version__
from .cost import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    CostTracker,
    PricingCatalog,
    timestamp_id,
)
from .docker_tests import DockerLimits, DockerTestRunner
from .errors import AgentError, ConfigError
from .github_ops import GitHubClient
from .git_ops import GitRepo, default_state_dir
from .security import is_within, safe_for_terminal, slugify
from .workflow import CodingWorkflow, RunReport, WorkflowConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description=(
            "Approval-gated AI coding agent for Git/GitHub repositories. "
            "Creates an isolated worktree, runs tests in Docker, and creates a "
            "local commit. Push and draft PR always require explicit approval."
        ),
    )
    parser.add_argument("repo", type=Path, help="Path to the target Git repository")
    parser.add_argument(
        "task",
        nargs="*",
        help="Task/instructions. Optional when --issue is supplied.",
    )
    parser.add_argument("--issue", type=int, help="GitHub issue number to implement")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Analyse and propose a plan only. No branch, worktree, file, commit, "
            "cost log or PR is created."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL") or "gpt-5.4",
        help="OpenAI model (default: OPENAI_MODEL or gpt-5.4)",
    )
    parser.add_argument(
        "--max-cost",
        default="2.00",
        metavar="USD",
        help="Client-side cost guard per task in USD (default: 2.00)",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
        help="Reasoning effort, only for models that declare support",
    )
    parser.add_argument(
        "--pricing-file",
        type=Path,
        help="Versioned pricing JSON file (default: bundled pricing.json)",
    )
    parser.add_argument(
        "--base-branch",
        help="Base branch for the agent branch (default: repository default)",
    )
    parser.add_argument(
        "--worktree-root",
        type=Path,
        help="Directory for isolated worktrees (default: state directory)",
    )
    parser.add_argument(
        "--cost-log-dir",
        type=Path,
        help="Directory for local per-task cost logs (default: state directory/costs)",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=300,
        help="Docker test timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--test-cpus",
        default="1.0",
        help="CPU limit for the test container (default: 1.0)",
    )
    parser.add_argument(
        "--test-memory",
        default="512m",
        help="Memory limit for the test container (default: 512m)",
    )
    parser.add_argument(
        "--test-pids",
        type=int,
        default=128,
        help="PID limit for the test container (default: 128)",
    )
    parser.add_argument(
        "--test-profile",
        default="pytest",
        help="Test profile (currently pytest)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def interactive_approver(prompt: str) -> bool:
    try:
        answer = input(f"\nAPPROVE {prompt}? [y/N] ")
    except EOFError:
        print("\nNo interactive input available; treating as DENIED.")
        return False
    return answer.strip().lower() in {"y", "yes"}


def _validate_limits(args: argparse.Namespace) -> DockerLimits:
    if args.test_timeout <= 0:
        raise ConfigError("--test-timeout must be greater than zero")
    if args.test_pids <= 0:
        raise ConfigError("--test-pids must be greater than zero")
    if not re.fullmatch(r"\d+(?:\.\d+)?", str(args.test_cpus)) or float(args.test_cpus) <= 0:
        raise ConfigError("--test-cpus must be a positive number, e.g. 1.0")
    if not re.fullmatch(r"\d+[mgMG]", str(args.test_memory)):
        raise ConfigError("--test-memory must look like 512m or 1g")
    return DockerLimits(
        cpus=str(args.test_cpus),
        memory=str(args.test_memory).lower(),
        pids=args.test_pids,
        timeout_seconds=args.test_timeout,
    )


def _cost_log_path(
    args: argparse.Namespace, repo: GitRepo, task_id: str
) -> Path:
    directory = (
        args.cost_log_dir.expanduser()
        if args.cost_log_dir
        else default_state_dir() / "costs"
    )
    unique = uuid.uuid4().hex[:8]
    return Path(directory) / (
        f"{timestamp_id()}-{os.getpid()}-{unique}-{task_id}.jsonl"
    )


def _print_report_cost_if_needed(report: RunReport, cost: CostTracker) -> None:
    # The full final report already includes cost after a commit. Dry-run,
    # cancellation and early failures need an explicit summary.
    if report.commit is None:
        print("\nCost summary:\n" + cost.format_summary(), flush=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: OpenAI | None = None,
    github: GitHubClient | None = None,
    docker: DockerTestRunner | None = None,
    approver: Callable[[str], bool] | None = None,
    echo: Callable[[str], None] = print,
    check_docker: bool = True,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cost: CostTracker | None = None

    try:
        task = " ".join(args.task).strip()
        if args.issue is not None and args.issue < 1:
            raise ConfigError("--issue must be a positive integer")
        if not task and args.issue is None:
            raise ConfigError("Provide a task or --issue.")
        try:
            max_cost = Decimal(str(args.max_cost))
        except (InvalidOperation, ValueError) as exc:
            raise ConfigError("--max-cost must be a decimal number, e.g. 2.00") from exc
        if not max_cost.is_finite() or max_cost <= 0:
            raise ConfigError("--max-cost must be greater than zero")
        limits = _validate_limits(args)

        repo = GitRepo(args.repo)
        if repo.path != args.repo.expanduser().resolve():
            print(
                f"Note: using Git repository root {repo.path}", flush=True
            )

        repo_task = (
            f"issue-{args.issue}"
            if args.issue is not None
            else f"task-{slugify(task, 30)}"
        )
        task_id = f"{slugify(repo.name, 24)}-{repo_task}"
        catalog = PricingCatalog.load(args.pricing_file)
        # Fail before any network call if the model price is unknown.
        catalog.get(args.model)

        api_key = os.getenv("OPENAI_API_KEY")
        if client is None and not api_key:
            raise ConfigError(
                "OPENAI_API_KEY is not set. Configure your OpenAI API key:\n"
                "  export OPENAI_API_KEY='sk-...'\n"
                "Use an API key from your OpenAI account; this is independent of "
                "Codex subscription limits. The key is never written to logs."
            )

        gh = github
        if gh is None:
            gh = GitHubClient(repo.path)
        if args.issue is not None and github is None:
            gh.check_available()

        docker_runner = docker
        if not args.dry_run:
            if docker_runner is None:
                docker_runner = DockerTestRunner(limits=limits)
            if check_docker and docker is None:
                docker_runner.ensure_available()

        log_path = None if args.dry_run else _cost_log_path(args, repo, task_id)
        if log_path is not None and is_within(repo.path, log_path):
            raise ConfigError(
                "--cost-log-dir must be outside the target repository. "
                "The default state directory is used when the flag is omitted."
            )
        cost = CostTracker(
            catalog,
            args.model,
            max_cost,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            log_path=log_path,
            task_id=task_id,
            issue=args.issue,
            repo_name=repo.name,
            echo=echo,
        )
        try:
            print(
                f"Model: {args.model} | max cost: ${cost.max_cost:.2f} | "
                f"pricing: {catalog.version}"
                + (" | DRY RUN" if args.dry_run else ""),
                flush=True,
            )
            if client is None:
                client = OpenAI(api_key=api_key)
            if args.dry_run:
                docker_runner = None

            config = WorkflowConfig(
                repo_path=repo.path,
                task=task,
                issue=args.issue,
                model=args.model,
                max_cost=max_cost,
                reasoning_effort=args.reasoning_effort,
                dry_run=args.dry_run,
                base_branch=args.base_branch,
                worktree_root=args.worktree_root,
                test_profile=args.test_profile,
            )
            workflow = CodingWorkflow(
                config,
                client=client,
                cost=cost,
                github=gh,
                docker=docker_runner,
                approver=approver or interactive_approver,
                echo=echo,
            )
            report = workflow.run()
            _print_report_cost_if_needed(report, cost)
            return 0 if report.success else 1
        finally:
            try:
                cost.close()
            except OSError as exc:
                print(
                    f"\nWARNING: could not finish the cost log: {exc}",
                    file=sys.stderr,
                )
    except KeyboardInterrupt:
        print("\nInterrupted by user. No automatic commit was made.", file=sys.stderr)
        if cost is not None:
            print("\nCost summary:\n" + cost.format_summary(), file=sys.stderr)
        return 130
    except AgentError as exc:
        print(f"\nERROR: {safe_for_terminal(str(exc), 8_000)}", file=sys.stderr)
        if exc.hint:
            print(safe_for_terminal(str(exc.hint), 2_000), file=sys.stderr)
        if cost is not None:
            print("\nCost summary:\n" + cost.format_summary(), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - defensive top-level guard
        print(
            f"\nUNEXPECTED ERROR ({type(exc).__name__}): "
            f"{safe_for_terminal(str(exc), 2_000)}",
            file=sys.stderr,
        )
        return 1


__all__ = ["build_parser", "interactive_approver", "main"]
