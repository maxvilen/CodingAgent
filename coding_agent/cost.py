"""Model pricing, token usage tracking, and client-side task budgets."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .errors import BudgetExceeded, ConfigError, PricingError

MILLION = Decimal(1_000_000)
DEFAULT_PRICING_FILE = Path(__file__).with_name("pricing.json")
DEFAULT_MAX_OUTPUT_TOKENS = 8_000


def _as_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PricingError(f"Invalid price for {field}: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise PricingError(f"Price for {field} must be a non-negative number")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ModelPricing:
    model: str
    input_price: Decimal
    cached_input_price: Decimal
    output_price: Decimal
    max_output_tokens: int | None = None
    reasoning_efforts: tuple[str, ...] = ()
    long_context_threshold: int | None = None
    long_context_input_price: Decimal | None = None
    long_context_cached_input_price: Decimal | None = None
    long_context_output_price: Decimal | None = None

    def rates_for_input(self, input_tokens: int) -> tuple[Decimal, Decimal, Decimal, bool]:
        if (
            self.long_context_threshold is not None
            and input_tokens > self.long_context_threshold
            and self.long_context_input_price is not None
            and self.long_context_cached_input_price is not None
            and self.long_context_output_price is not None
        ):
            return (
                self.long_context_input_price,
                self.long_context_cached_input_price,
                self.long_context_output_price,
                True,
            )
        return self.input_price, self.cached_input_price, self.output_price, False

    def calculate(self, usage: "TokenUsage") -> Decimal:
        input_price, cached_price, output_price, _ = self.rates_for_input(
            usage.input_tokens
        )
        cached = max(0, min(usage.input_tokens, usage.cached_tokens))
        uncached = max(0, usage.input_tokens - cached)
        total = (
            Decimal(uncached) * input_price
            + Decimal(cached) * cached_price
            + Decimal(max(0, usage.output_tokens)) * output_price
        )
        return total / MILLION


class PricingCatalog:
    """Versioned pricing loaded from an editable JSON file."""

    def __init__(
        self,
        models: dict[str, ModelPricing],
        *,
        version: str,
        source: str,
        path: Path,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.models = models
        self.version = version
        self.source = source
        self.path = path
        self.aliases = aliases or {}

    @classmethod
    def load(cls, path: Path | None = None) -> "PricingCatalog":
        pricing_path = Path(path) if path is not None else DEFAULT_PRICING_FILE
        try:
            raw = json.loads(pricing_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PricingError(f"Pricing file not found: {pricing_path}") from exc
        except json.JSONDecodeError as exc:
            raise PricingError(
                f"Pricing file is not valid JSON: {pricing_path} ({exc.msg})"
            ) from exc

        version = str(raw.get("version") or "").strip()
        source = str(raw.get("source") or "").strip()
        if not version or not source:
            raise PricingError(
                "Pricing file must contain a version and a source URL "
                f"({pricing_path})"
            )
        if raw.get("currency") != "USD":
            raise PricingError("Pricing file currency must be USD")
        if raw.get("unit") != "per 1M tokens":
            raise PricingError("Pricing file unit must be 'per 1M tokens'")

        raw_models = raw.get("models")
        if not isinstance(raw_models, dict) or not raw_models:
            raise PricingError("Pricing file contains no models")

        models: dict[str, ModelPricing] = {}
        for name, data in raw_models.items():
            if not isinstance(data, dict):
                raise PricingError(f"Pricing entry for {name} must be an object")
            long_threshold = data.get("long_context_threshold")
            long_values = (
                data.get("long_context_input"),
                data.get("long_context_cached_input"),
                data.get("long_context_output"),
            )
            if long_threshold is None and any(v is not None for v in long_values):
                raise PricingError(
                    f"Model {name} has long-context prices but no threshold"
                )
            if long_threshold is not None and any(v is None for v in long_values):
                raise PricingError(
                    f"Model {name} has a long-context threshold but incomplete prices"
                )
            efforts = data.get("reasoning_efforts") or []
            if not isinstance(efforts, list) or any(
                not isinstance(e, str) for e in efforts
            ):
                raise PricingError(f"Invalid reasoning_efforts for {name}")
            max_output = data.get("max_output_tokens")
            if max_output is not None and (
                not isinstance(max_output, int) or max_output <= 0
            ):
                raise PricingError(f"Invalid max_output_tokens for {name}")
            models[name] = ModelPricing(
                model=name,
                input_price=_as_decimal(data.get("input"), f"{name}.input"),
                cached_input_price=_as_decimal(
                    data.get("cached_input"), f"{name}.cached_input"
                ),
                output_price=_as_decimal(data.get("output"), f"{name}.output"),
                max_output_tokens=max_output,
                reasoning_efforts=tuple(efforts),
                long_context_threshold=long_threshold,
                long_context_input_price=(
                    _as_decimal(long_values[0], f"{name}.long_context_input")
                    if long_values[0] is not None
                    else None
                ),
                long_context_cached_input_price=(
                    _as_decimal(
                        long_values[1], f"{name}.long_context_cached_input"
                    )
                    if long_values[1] is not None
                    else None
                ),
                long_context_output_price=(
                    _as_decimal(long_values[2], f"{name}.long_context_output")
                    if long_values[2] is not None
                    else None
                ),
            )

        aliases_raw = raw.get("aliases") or {}
        if not isinstance(aliases_raw, dict):
            raise PricingError("Pricing aliases must be an object")
        aliases = {str(k): str(v) for k, v in aliases_raw.items()}

        return cls(
            models,
            version=version,
            source=source,
            path=pricing_path,
            aliases=aliases,
        )

    def get(self, model: str) -> ModelPricing:
        resolved = self.aliases.get(model, model)
        pricing = self.models.get(resolved)
        if pricing is None:
            known = ", ".join(sorted(self.models))
            raise PricingError(
                f"No pricing is configured for model '{model}'. "
                f"Pricing file version {self.version} ({self.path}). "
                f"Known models: {known}. Add an explicit price entry or use "
                "another --model; unknown prices are never assumed to be zero."
            )
        return pricing


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_response(cls, usage: Any) -> "TokenUsage | None":
        if usage is None:
            return None

        def value(obj: Any, name: str, default: int = 0) -> int:
            if obj is None:
                return default
            if isinstance(obj, dict):
                raw = obj.get(name, default)
            else:
                raw = getattr(obj, name, default)
            try:
                return max(0, int(raw or 0))
            except (TypeError, ValueError):
                return default

        input_details = (
            usage.get("input_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "input_tokens_details", None)
        )
        output_details = (
            usage.get("output_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "output_tokens_details", None)
        )
        return cls(
            input_tokens=value(usage, "input_tokens"),
            output_tokens=value(usage, "output_tokens"),
            cached_tokens=value(input_details, "cached_tokens"),
            reasoning_tokens=value(output_details, "reasoning_tokens"),
        )


@dataclass(frozen=True)
class CostEstimate:
    input_tokens: int
    output_tokens: int
    cost: Decimal
    long_context: bool


@dataclass(frozen=True)
class CostEntry:
    usage: TokenUsage
    cost: Decimal
    cumulative: Decimal
    estimated: bool = False


@dataclass(frozen=True)
class CostSummary:
    model: str
    pricing_version: str
    max_cost: Decimal
    total_cost: Decimal
    calls: int
    usage: TokenUsage
    log_path: Path | None

    @property
    def remaining(self) -> Decimal:
        return self.max_cost - self.total_cost


def _serialize_for_estimate(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(k): _serialize_for_estimate(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_for_estimate(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class CostTracker:
    """Tracks estimated and reported API cost for one task.

    The budget is a *client-side guard*, not a hard billing limit. The cost of
    an in-flight request is unknown until its usage has been returned.
    """

    def __init__(
        self,
        catalog: PricingCatalog,
        model: str,
        max_cost: Decimal | str | float,
        *,
        reasoning_effort: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        log_path: Path | None = None,
        task_id: str = "task",
        issue: int | None = None,
        repo_name: str = "",
        echo: Callable[[str], None] = print,
    ) -> None:
        self.catalog = catalog
        self.model = model
        self.pricing = catalog.get(model)
        try:
            self.max_cost = Decimal(str(max_cost))
        except (InvalidOperation, ValueError) as exc:
            raise ConfigError("--max-cost must be a decimal number") from exc
        if not self.max_cost.is_finite() or self.max_cost <= 0:
            raise ConfigError("--max-cost must be greater than zero")
        self.reasoning_effort = reasoning_effort
        if reasoning_effort:
            if reasoning_effort not in self.pricing.reasoning_efforts:
                supported = ", ".join(self.pricing.reasoning_efforts) or "none"
                raise ConfigError(
                    f"Model {model} does not declare reasoning effort "
                    f"'{reasoning_effort}' in the pricing file. Supported: {supported}"
                )
        self.max_output_tokens = max_output_tokens
        if self.pricing.max_output_tokens is not None:
            self.max_output_tokens = min(
                self.max_output_tokens, self.pricing.max_output_tokens
            )
        self.task_id = task_id
        self.issue = issue
        self.repo_name = repo_name
        self.echo = echo
        self.calls = 0
        self.total_cost = Decimal(0)
        self.total_usage = TokenUsage()
        self._last_estimate: CostEstimate | None = None
        self._log_path: Path | None = Path(log_path) if log_path else None
        self._closed = False
        if self._log_path is not None:
            self._prepare_log()

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    @property
    def remaining(self) -> Decimal:
        return self.max_cost - self.total_cost

    def _prepare_log(self) -> None:
        log_path = self._log_path
        if log_path is None:
            return
        try:
            parent_existed = log_path.parent.exists()
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_existed:
                os.chmod(log_path.parent, 0o700)
            fd = os.open(
                log_path,
                os.O_CREAT | os.O_WRONLY | os.O_APPEND,
                0o600,
            )
            os.close(fd)
            os.chmod(log_path, 0o600)
            self._write_log(
                {
                    "event": "start",
                    "task_id": self.task_id,
                    "issue": self.issue,
                    "repo": self.repo_name,
                    "model": self.model,
                    "pricing_version": self.catalog.version,
                    "pricing_source": self.catalog.source,
                    "max_cost_usd": str(self.max_cost),
                    "max_output_tokens": self.max_output_tokens,
                }
            )
        except OSError as exc:
            raise ConfigError(
                f"Could not create cost log at {log_path}: {exc.strerror or exc}"
            ) from exc

    def _write_log(self, payload: dict[str, Any]) -> None:
        if self._log_path is None:
            return
        record = {"timestamp": _utc_now(), **payload}
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def estimate_request(
        self,
        instructions: str,
        messages: list[Any],
        *,
        max_output_tokens: int | None = None,
    ) -> CostEstimate:
        rendered = json.dumps(
            {
                "instructions": instructions,
                "input": _serialize_for_estimate(messages),
            },
            ensure_ascii=False,
            default=str,
        )
        # Deliberately conservative: ~2.8 characters per token after the 25%
        # margin, plus request framing. Code and non-English text can tokenize
        # worse than the common 4-characters-per-token rule of thumb.
        estimated_input = max(1_000, math.ceil(len(rendered) / 3.5 * 1.25) + 400)
        output_cap = max_output_tokens or self.max_output_tokens
        if self.pricing.max_output_tokens is not None:
            output_cap = min(output_cap, self.pricing.max_output_tokens)
        # Use the higher long-context tier slightly before the threshold to
        # avoid under-estimating a request that lands just above it.
        rate_input = estimated_input
        threshold = self.pricing.long_context_threshold
        if threshold is not None and estimated_input > int(threshold * 0.9):
            rate_input = threshold + 1
        input_price, _cached_price, output_price, long_context = (
            self.pricing.rates_for_input(rate_input)
        )
        cost = (
            Decimal(estimated_input) * input_price
            + Decimal(max(0, output_cap)) * output_price
        ) / MILLION
        return CostEstimate(
            input_tokens=estimated_input,
            output_tokens=output_cap,
            cost=cost,
            long_context=long_context,
        )

    def prepare_call(
        self,
        instructions: str,
        messages: list[Any],
        *,
        label: str = "api-call",
        max_output_tokens: int | None = None,
    ) -> CostEstimate:
        """Check the conservative worst-case estimate before an API call."""

        estimate = self.estimate_request(
            instructions, messages, max_output_tokens=max_output_tokens
        )
        projected = self.total_cost + estimate.cost
        if self.total_cost >= self.max_cost:
            raise BudgetExceeded(
                f"Cost limit reached (${self.total_cost:.6f} of "
                f"${self.max_cost:.2f}). No new API call was made. This is a "
                "client-side budget guard, not a guaranteed billing cap."
            )
        if projected > self.max_cost:
            raise BudgetExceeded(
                "The conservative worst-case estimate for the next API call "
                f"(${estimate.cost:.6f}) would exceed the remaining budget "
                f"(${self.remaining:.6f} of ${self.max_cost:.2f}). No new API "
                "call was made. Increase --max-cost if this is intended. This "
                "is a client-side budget guard, not a guaranteed billing cap."
            )
        self._last_estimate = estimate
        self._write_log(
            {
                "event": "precall",
                "label": label,
                "estimated_input_tokens": estimate.input_tokens,
                "estimated_max_output_tokens": estimate.output_tokens,
                "estimated_cost_usd": str(estimate.cost),
                "projected_total_usd": str(projected),
                "long_context": estimate.long_context,
            }
        )
        return estimate

    def record_usage(self, usage: Any, *, label: str = "api-call") -> CostEntry:
        token_usage = TokenUsage.from_response(usage)
        estimated = token_usage is None
        if token_usage is None:
            estimate = self._last_estimate or CostEstimate(0, 0, Decimal(0), False)
            token_usage = TokenUsage(
                input_tokens=estimate.input_tokens,
                output_tokens=estimate.output_tokens,
            )
            cost = estimate.cost
            self.echo(
                "WARNING: the API returned no usage data; using the conservative "
                "pre-call estimate for this request."
            )
        else:
            cost = self.pricing.calculate(token_usage)

        self.calls += 1
        self.total_cost += cost
        self.total_usage = TokenUsage(
            input_tokens=self.total_usage.input_tokens + token_usage.input_tokens,
            output_tokens=self.total_usage.output_tokens + token_usage.output_tokens,
            cached_tokens=self.total_usage.cached_tokens + token_usage.cached_tokens,
            reasoning_tokens=self.total_usage.reasoning_tokens
            + token_usage.reasoning_tokens,
        )
        entry = CostEntry(
            usage=token_usage,
            cost=cost,
            cumulative=self.total_cost,
            estimated=estimated,
        )
        self._write_log(
            {
                "event": "usage",
                "label": label,
                "input_tokens": token_usage.input_tokens,
                "output_tokens": token_usage.output_tokens,
                "cached_tokens": token_usage.cached_tokens,
                "reasoning_tokens": token_usage.reasoning_tokens,
                "cost_usd": str(cost),
                "total_cost_usd": str(self.total_cost),
                "estimated_fallback": estimated,
            }
        )
        self.echo(self.format_usage(entry))
        if self.total_cost > self.max_cost:
            self.echo(
                "WARNING: reported cost has exceeded --max-cost. No further API "
                "calls will be made. Actual provider billing may differ."
            )
        return entry

    def format_usage(self, entry: CostEntry) -> str:
        used_pct = (
            (entry.cumulative / self.max_cost * 100) if self.max_cost > 0 else Decimal(0)
        )
        cached = (
            f" / cached {entry.usage.cached_tokens:,}"
            if entry.usage.cached_tokens
            else ""
        )
        reasoning = (
            f" / reasoning {entry.usage.reasoning_tokens:,}"
            if entry.usage.reasoning_tokens
            else ""
        )
        return (
            f"Cost: ${entry.cost:.6f} "
            f"(input {entry.usage.input_tokens:,}{cached} / "
            f"output {entry.usage.output_tokens:,}{reasoning}) | "
            f"total ${entry.cumulative:.6f} / ${self.max_cost:.2f} "
            f"({used_pct:.1f}%)"
        )

    def summary(self) -> CostSummary:
        return CostSummary(
            model=self.model,
            pricing_version=self.catalog.version,
            max_cost=self.max_cost,
            total_cost=self.total_cost,
            calls=self.calls,
            usage=self.total_usage,
            log_path=self._log_path,
        )

    def format_summary(self) -> str:
        summary = self.summary()
        cached = (
            f", cached {summary.usage.cached_tokens:,}"
            if summary.usage.cached_tokens
            else ""
        )
        reasoning = (
            f", reasoning {summary.usage.reasoning_tokens:,}"
            if summary.usage.reasoning_tokens
            else ""
        )
        log = f"\n  Cost log: {summary.log_path}" if summary.log_path else ""
        return (
            f"  Model: {summary.model} (pricing {summary.pricing_version})\n"
            f"  API calls: {summary.calls}\n"
            f"  Tokens: input {summary.usage.input_tokens:,}{cached}, "
            f"output {summary.usage.output_tokens:,}{reasoning}\n"
            f"  Estimated cost: ${summary.total_cost:.6f} of "
            f"${summary.max_cost:.2f}{log}\n"
            "  Note: estimated from API usage; actual provider billing may differ. "
            "The budget is a client-side guard, not a guaranteed billing limit."
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._write_log(
            {
                "event": "end",
                "total_cost_usd": str(self.total_cost),
                "calls": self.calls,
                "input_tokens": self.total_usage.input_tokens,
                "output_tokens": self.total_usage.output_tokens,
                "cached_tokens": self.total_usage.cached_tokens,
                "reasoning_tokens": self.total_usage.reasoning_tokens,
            }
        )

    def __enter__(self) -> "CostTracker":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def timestamp_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())
