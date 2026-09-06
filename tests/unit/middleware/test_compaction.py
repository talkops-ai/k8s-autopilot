"""Unit tests for CLICompactionMiddleware."""

from __future__ import annotations

from unittest.mock import MagicMock

from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from k8s_autopilot.middleware.compaction import (
    CLICompactionMiddleware,
    _create_cli_compaction_middleware,
    _offload_seed_message_id,
    _without_offload_seed,
)


class TestCompactionMiddleware:
    def test_seed_message_id(self) -> None:
        assert _offload_seed_message_id("call-123") == "offload-seed-call-123"

    def test_without_offload_seed(self) -> None:
        messages = [
            {"id": "msg-1", "content": "hello"},
            {"id": "offload-seed-call-1", "content": "seed"},
            {"id": "msg-2", "content": "world"},
        ]
        filtered = _without_offload_seed(messages, "call-1")
        assert len(filtered) == 2
        assert filtered[0]["id"] == "msg-1"
        assert filtered[1]["id"] == "msg-2"

    def test_create_cli_compaction_middleware(self, tmp_path) -> None:
        model = GenericFakeChatModel(messages=iter([]))
        backend = FilesystemBackend(virtual_mode=False)
        mw = _create_cli_compaction_middleware(model, backend)
        assert isinstance(mw, CLICompactionMiddleware)
        assert mw.name == "SummarizationMiddleware"

