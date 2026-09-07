"""Grader tools and isolation for rubric evaluation."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Any
from langchain_core.tools import BaseTool, tool
from deepagents.middleware import GRADER_SYSTEM_PROMPT

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_RUBRIC_GRADER_READ_FILE_PREFIX = "/large_tool_results/"

_RUBRIC_GRADER_SYSTEM_PROMPT = (
    GRADER_SYSTEM_PROMPT
    + "\n\nWhen the transcript says a tool result was saved under "
    + f"`{_RUBRIC_GRADER_READ_FILE_PREFIX}`, use the `read_file` tool to inspect "
    + "the referenced evidence before deciding that a criterion lacks support. "
    + "Only read paths that are explicitly present in the transcript."
)


def _validate_rubric_grader_read_path(file_path: str) -> str | None:
    normalized = file_path.replace("\\", "/")
    if not normalized.startswith(_RUBRIC_GRADER_READ_FILE_PREFIX):
        return "Rubric grader can only read files under /large_tool_results/."
    parts = PurePosixPath(normalized).parts
    if ".." in parts or "~" in parts:
        return "Invalid path."
    return None


def _create_rubric_grader_tools(backend: Any = None) -> list[BaseTool]:
    """Create a read_file tool constrained to only read /large_tool_results/ paths."""

    @tool
    def read_file(file_path: str, offset: int = 0, limit: int = 100) -> str:
        """Read an offloaded tool result referenced in the transcript.

        Returns:
            The file content, or an error message when the path is
            outside the grader evidence directory.
        """
        if error := _validate_rubric_grader_read_path(file_path):
            return error

        try:
            p = Path(file_path)
            if not p.exists():
                return f"File {file_path} not found."
            with open(p, "r", encoding="utf-8") as f:
                content = f.read()
            lines = content.splitlines()
            sliced = lines[offset : offset + limit]
            return "\n".join(sliced)
        except Exception as e:
            return f"Error reading file {file_path}: {e}"

    return [read_file]
