from __future__ import annotations

import json
import os
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from coding_agent.cost import (
    CostTracker,
    PricingCatalog,
    TokenUsage,
    DEFAULT_PRICING_FILE,
)
from coding_agent.errors import BudgetExceeded, ConfigError, PricingError


def test_bundled_pricing_is_versioned_and_known() -> None:
    catalog = PricingCatalog.load()
    assert catalog.version
    assert catalog.source.startswith("https://")
    pricing = catalog.get("gpt-5.4")
    assert pricing.input_price == Decimal("2.5")
    assert pricing.output_price == Decimal("15")
    assert pricing.cached_input_price == Decimal("0.25")
    assert "medium" in pricing.reasoning_efforts
    assert catalog.get("gpt-5.4-2026-03-05").model == "gpt-5.4"


def test_unknown_model_is_an_explicit_error() -> None:
    catalog = PricingCatalog.load()
    with pytest.raises(PricingError, match="No pricing is configured"):
        catalog.get("unknown-model-xyz")
    with pytest.raises(PricingError, match="never assumed to be zero"):
        catalog.get("unknown-model-xyz")


def test_cost_calculation_with_cached_tokens() -> None:
    catalog = PricingCatalog.load()
    pricing = catalog.get("gpt-5.4")
    usage = TokenUsage(
        input_tokens=200_000,
        output_tokens=50_000,
        cached_tokens=50_000,
    )
    # 150k * 2.50 + 50k * 0.25 + 50k * 15 = 1.1375
    assert pricing.calculate(usage) == Decimal("1.1375")


def test_long_context_tier() -> None:
    catalog = PricingCatalog.load()
    pricing = catalog.get("gpt-5.4")
    usage = TokenUsage(input_tokens=300_000, output_tokens=10_000)
    expected = (
        Decimal(300_000) * Decimal("5")
        + Decimal(10_000) * Decimal("22.5")
    ) / Decimal(1_000_000)
    assert pricing.calculate(usage) == expected


def test_custom_pricing_file(tmp_path: Path) -> None:
    pricing = {
        "version": "test-1",
        "currency": "USD",
        "unit": "per 1M tokens",
        "source": "https://example.invalid/pricing",
        "models": {
            "test-model": {"input": 1, "cached_input": 0.5, "output": 2}
        },
    }
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(pricing), encoding="utf-8")
    catalog = PricingCatalog.load(path)
    assert catalog.get("test-model").output_price == Decimal("2")
    with pytest.raises(PricingError):
        catalog.get("gpt-5.4")


def test_cost_tracker_records_usage_and_writes_safe_log(tmp_path: Path) -> None:
    log = tmp_path / "costs" / "task.jsonl"
    tracker = CostTracker(
        PricingCatalog.load(),
        "gpt-5.4",
        "2.00",
        log_path=log,
        task_id="demo-issue-1",
        issue=1,
        repo_name="demo",
        echo=lambda _text: None,
    )
    tracker.prepare_call("system", [{"role": "user", "content": "SECRET PROMPT"}])
    entry = tracker.record_usage(
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "input_tokens_details": {"cached_tokens": 100},
            "output_tokens_details": {"reasoning_tokens": 50},
        }
    )
    assert entry.cost > 0
    tracker.close()

    assert log.exists()
    mode = stat.S_IMODE(os.stat(log).st_mode)
    assert mode == 0o600
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[0]["event"] == "start"
    assert any(record["event"] == "precall" for record in records)
    assert any(record["event"] == "usage" for record in records)
    assert records[-1]["event"] == "end"
    assert "SECRET PROMPT" not in log.read_text()
    assert records[0]["pricing_version"] == tracker.catalog.version


def test_budget_guard_blocks_before_conservative_estimate() -> None:
    tracker = CostTracker(
        PricingCatalog.load(),
        "gpt-5.4",
        "0.01",
        echo=lambda _text: None,
    )
    with pytest.raises(BudgetExceeded, match="client-side budget guard"):
        tracker.prepare_call("system", [{"role": "user", "content": "x" * 1000}])
    assert tracker.calls == 0


def test_budget_guard_blocks_subsequent_calls_after_reported_overage() -> None:
    tracker = CostTracker(
        PricingCatalog.load(),
        "gpt-5.4",
        "1.00",
        echo=lambda _text: None,
    )
    tracker.prepare_call("system", [{"role": "user", "content": "hello"}])
    tracker.record_usage(
        {"input_tokens": 1_000_000, "output_tokens": 100_000}
    )
    assert tracker.total_cost > tracker.max_cost
    with pytest.raises(BudgetExceeded, match="Cost limit reached"):
        tracker.prepare_call("system", [{"role": "user", "content": "again"}])


def test_missing_usage_falls_back_to_estimate() -> None:
    messages: list[str] = []
    tracker = CostTracker(
        PricingCatalog.load(),
        "gpt-5.4",
        "2.00",
        echo=messages.append,
    )
    tracker.prepare_call("system", [{"role": "user", "content": "hello"}])
    entry = tracker.record_usage(None)
    assert entry.estimated is True
    assert entry.cost > 0
    assert any("no usage data" in line for line in messages)


def test_reasoning_effort_validation() -> None:
    catalog = PricingCatalog.load()
    CostTracker(
        catalog, "gpt-5.4", "2.00", reasoning_effort="medium", echo=lambda _t: None
    )
    with pytest.raises(ConfigError, match="does not declare reasoning effort"):
        CostTracker(
            catalog, "gpt-4.1", "2.00", reasoning_effort="medium", echo=lambda _t: None
        )


def test_max_cost_must_be_positive() -> None:
    with pytest.raises(ConfigError):
        CostTracker(PricingCatalog.load(), "gpt-5.4", "0")
    with pytest.raises(ConfigError):
        CostTracker(PricingCatalog.load(), "gpt-5.4", "abc")


def test_pricing_file_requires_version_and_source(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "currency": "USD",
                "unit": "per 1M tokens",
                "models": {"x": {"input": 1, "cached_input": 1, "output": 1}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PricingError, match="version and a source"):
        PricingCatalog.load(path)


def test_bundled_pricing_file_path() -> None:
    assert DEFAULT_PRICING_FILE.name == "pricing.json"
    assert DEFAULT_PRICING_FILE.exists()
