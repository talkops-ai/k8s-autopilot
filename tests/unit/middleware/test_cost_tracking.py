"""Unit tests for CostTrackingMiddleware and pricing integration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from k8s_autopilot.middleware.cost_tracking import (
    CostState,
    CostTrackingMiddleware,
    estimate_cost,
    pricing_data_available,
    resolve_message_model,
)


class TestCostTrackingPricing:
    def test_pricing_data_available(self) -> None:
        assert isinstance(pricing_data_available(), bool)

    def test_estimate_cost_valid_usage(self) -> None:
        usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "input_token_details": {"cache_read": 100},
            "output_token_details": {"reasoning": 50},
        }
        # Testing estimate_cost with standard models (e.g. gpt-4o)
        cost = estimate_cost(usage, "gpt-4o", "openai")
        # If genai-prices has pricing for gpt-4o, cost is float > 0
        if cost is not None:
            assert isinstance(cost, float)
            assert cost > 0.0

    def test_estimate_cost_empty_usage(self) -> None:
        assert estimate_cost(None, "gpt-4o") is None
        assert estimate_cost({}, "gpt-4o") is None
        assert estimate_cost({"input_tokens": 0, "output_tokens": 0}, "gpt-4o") is None

    def test_estimate_cost_unpriceable_provider(self) -> None:
        usage = {"input_tokens": 100, "output_tokens": 50}
        assert estimate_cost(usage, "code-model", "openai_codex") is None

    def test_resolve_message_model(self) -> None:
        msg = AIMessage(
            content="Hello",
            response_metadata={"model_name": "claude-3-5-sonnet-20241022", "model_provider": "anthropic"},
        )
        model, provider = resolve_message_model(msg, fallback_model="default", fallback_provider="default_p")
        assert model == "claude-3-5-sonnet-20241022"
        assert provider == "anthropic"

    def test_resolve_message_model_fallback(self) -> None:
        msg = AIMessage(content="Hello")
        model, provider = resolve_message_model(msg, fallback_model="gpt-4o", fallback_provider="openai")
        assert model == "gpt-4o"
        assert provider == "openai"


class TestCostTrackingMiddleware:
    def test_instantiation(self) -> None:
        mw = CostTrackingMiddleware()
        assert not mw._nested
        assert mw.state_schema is CostState

    def test_before_agent_nested(self) -> None:
        mw = CostTrackingMiddleware(nested=True)
        runtime = MagicMock()
        update = mw.before_agent({}, runtime)
        assert update is not None
        assert update.get("_session_cost_usd") is not None
        assert update["_session_cost_usd"].value == 0.0

    def test_before_agent_top_level(self) -> None:
        mw = CostTrackingMiddleware(nested=False)
        runtime = MagicMock()
        update = mw.before_agent({}, runtime)
        assert update is None

    def test_after_model_empty(self) -> None:
        mw = CostTrackingMiddleware()
        runtime = MagicMock()
        runtime.execution_info.thread_id = "test-thread"
        state = {"messages": []}
        update = mw.after_model(state, runtime)
        assert update is None
