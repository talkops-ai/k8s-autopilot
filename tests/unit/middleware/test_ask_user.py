"""Unit tests for the ask_user middleware — question validation, answer parsing, and middleware hooks."""

from __future__ import annotations

import pytest


class TestQuestionValidation:
    def test_empty_list_rejected(self):
        from k8s_autopilot.middleware.ask_user import _validate_questions

        with pytest.raises(ValueError, match="at least one question"):
            _validate_questions([])

    def test_invalid_question_type_rejected(self):
        from k8s_autopilot.middleware.ask_user import _validate_questions

        with pytest.raises(ValueError, match="unsupported.*type"):
            _validate_questions([{"question": "hi", "type": "radio"}])

    def test_multiple_choice_without_choices_rejected(self):
        from k8s_autopilot.middleware.ask_user import _validate_questions

        with pytest.raises(ValueError, match="requires non-empty.*choices"):
            _validate_questions(
                [{"question": "Pick one", "type": "multiple_choice"}]
            )

    def test_text_with_choices_rejected(self):
        from k8s_autopilot.middleware.ask_user import _validate_questions

        with pytest.raises(ValueError, match="must not define.*choices"):
            _validate_questions(
                [{"question": "Name?", "type": "text", "choices": [{"value": "A"}]}]
            )

    def test_valid_mixed_questions(self):
        from k8s_autopilot.middleware.ask_user import _validate_questions

        _validate_questions([
            {"question": "Your name?", "type": "text"},
            {
                "question": "Choose framework",
                "type": "multiple_choice",
                "choices": [{"value": "React"}, {"value": "Vue"}],
            },
        ])

    def test_empty_question_text_rejected(self):
        from k8s_autopilot.middleware.ask_user import _validate_questions

        with pytest.raises(ValueError, match="non-empty"):
            _validate_questions([{"question": "   ", "type": "text"}])


class TestAnswerParsing:
    def test_successful_answers(self):
        from k8s_autopilot.middleware.ask_user import _parse_answers

        questions = [{"question": "Name?", "type": "text"}]
        response = {"status": "answered", "answers": ["John"]}
        result = _parse_answers(response, questions, "tc-1")
        content = result.update["messages"][0].content
        assert "Q: Name?" in content
        assert "A: John" in content

    def test_cancelled_response(self):
        from k8s_autopilot.middleware.ask_user import _parse_answers

        questions = [{"question": "Name?", "type": "text"}]
        result = _parse_answers({"status": "cancelled"}, questions, "tc-1")
        assert "(cancelled)" in result.update["messages"][0].content

    def test_string_response(self):
        from k8s_autopilot.middleware.ask_user import _parse_answers

        questions = [{"question": "Name?", "type": "text"}]
        result = _parse_answers("John", questions, "tc-1")
        assert "Q: Name?" in result.update["messages"][0].content
        assert "A: John" in result.update["messages"][0].content

    def test_list_response(self):
        from k8s_autopilot.middleware.ask_user import _parse_answers

        questions = [{"question": "Name?", "type": "text"}]
        result = _parse_answers(["Alice"], questions, "tc-1")
        assert "A: Alice" in result.update["messages"][0].content

    def test_error_status_payload(self):
        from k8s_autopilot.middleware.ask_user import _parse_answers

        questions = [{"question": "Name?", "type": "text"}]
        result = _parse_answers({"status": "error", "error": "timeout"}, questions, "tc-1")
        assert "(error: timeout)" in result.update["messages"][0].content

    def test_missing_answers_key_defaults_to_error(self):
        from k8s_autopilot.middleware.ask_user import _parse_answers

        questions = [{"question": "Name?", "type": "text"}]
        result = _parse_answers({"status": "answered"}, questions, "tc-1")
        assert "(error:" in result.update["messages"][0].content


    def test_multiple_questions_and_answers(self):
        from k8s_autopilot.middleware.ask_user import _parse_answers

        questions = [
            {"question": "Name?", "type": "text"},
            {"question": "Framework?", "type": "text"},
        ]
        response = {"status": "answered", "answers": ["Alice", "FastAPI"]}
        result = _parse_answers(response, questions, "tc-1")
        content = result.update["messages"][0].content
        assert "Q: Name?" in content
        assert "A: Alice" in content
        assert "Q: Framework?" in content
        assert "A: FastAPI" in content


class TestAskUserMiddleware:
    def test_instantiation(self):
        from k8s_autopilot.middleware.ask_user import AskUserMiddleware

        mw = AskUserMiddleware()
        assert len(mw.tools) == 1
        assert mw.tools[0].name == "ask_user"

    def test_custom_description(self):
        from k8s_autopilot.middleware.ask_user import AskUserMiddleware

        mw = AskUserMiddleware(tool_description="Custom description")
        assert mw.tool_description == "Custom description"
