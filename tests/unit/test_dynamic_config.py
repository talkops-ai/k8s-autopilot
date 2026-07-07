import pytest
from unittest.mock import patch
from k8s_autopilot.config.config import Config
from k8s_autopilot.config.db_config import (
    deserialize_value,
    serialize_value,
    _determine_category,
    _determine_type,
    _is_sensitive
)

def test_value_serialization():
    # Test boolean serialization/deserialization
    assert serialize_value(True, "bool") == "true"
    assert serialize_value(False, "bool") == "false"
    assert deserialize_value("true", "bool") is True
    assert deserialize_value("false", "bool") is False

    # Test JSON serialization/deserialization
    json_val = {"a": 1, "b": [2, 3]}
    ser = serialize_value(json_val, "json")
    assert deserialize_value(ser, "json") == json_val

    # Test int/float/str
    assert serialize_value(42, "int") == "42"
    assert deserialize_value("42", "int") == 42
    assert serialize_value(3.14, "float") == "3.14"
    assert deserialize_value("3.14", "float") == 3.14
    assert serialize_value("hello", "str") == "hello"
    assert deserialize_value("hello", "str") == "hello"

def test_helpers():
    assert _determine_category("LLM_PROVIDER") == "llm"
    assert _determine_category("SLACK_BOT_TOKEN") == "integration"
    assert _determine_category("PROMETHEUS_BASE_URL") == "mcp"
    assert _determine_category("LOG_LEVEL") == "system"

    assert _determine_type(True) == "bool"
    assert _determine_type(42) == "int"
    assert _determine_type(3.14) == "float"
    assert _determine_type({"a": 1}) == "json"
    assert _determine_type("hello") == "str"

    assert _is_sensitive("SLACK_BOT_TOKEN") is True
    assert _is_sensitive("GITHUB_PERSONAL_ACCESS_TOKEN") is True
    assert _is_sensitive("LOG_LEVEL") is False
    assert _is_sensitive("LLM_DEEPAGENT_MAX_TOKENS") is False

class MockCursor:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query, params=None):
        pass

    async def fetchall(self):
        return self.rows

class MockConnection:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def cursor(self, row_factory=None):
        return MockCursor(self.rows)

@pytest.mark.asyncio
@patch("psycopg.AsyncConnection.connect")
@patch("k8s_autopilot.core.hitl.checkpointer.get_database_uri")
async def test_config_reload(mock_get_db_uri, mock_connect):
    mock_get_db_uri.return_value = "postgresql://mock_db"
    
    # Set up mock rows to return
    rows = [
        ("LLM_TEMPERATURE", "0.5", "float", None),
        ("LOG_LEVEL", "DEBUG", "str", None),
        ("SLACK_ENABLED", "true", "bool", None),
    ]
    
    mock_connect.return_value = MockConnection(rows)

    # Instantiate config
    cfg = Config()
    
    # Assert initial temperature is from defaults (usually 0.0)
    assert cfg.LLM_TEMPERATURE != 0.5
    
    # Reload from mock DB
    await cfg.reload()
    
    # Assert overrides applied
    assert cfg.LLM_TEMPERATURE == 0.5
    assert cfg.LOG_LEVEL == "DEBUG"
    assert cfg.SLACK_ENABLED is True
    
    # Test to_dict includes overrides
    merged = cfg.to_dict()
    assert merged["LLM_TEMPERATURE"] == 0.5
    assert merged["LOG_LEVEL"] == "DEBUG"
    assert merged["SLACK_ENABLED"] is True
