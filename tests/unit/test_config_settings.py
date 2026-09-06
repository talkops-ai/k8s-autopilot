"""Unit tests for K8s Autopilot Config & Settings Module.

Tests the manifest-driven, DB-backed config architecture:
- Paths & directory helpers
- Settings dataclass (from_env bootstrap)
- Manifest registry (ConfigOption, OptionKind)
- Type coercion (coerce_str_value)
- Env-var resolution (resolve_from_env)
"""

import os
import pytest
from pathlib import Path

from k8s_autopilot.config.manifest import (
    ConfigOption,
    OptionKind,
    coerce_str_value,
    get_config_options,
    get_option,
    get_option_by_db_key,
    iter_groups,
    option_keys,
    resolve_from_env,
    serialize_typed_value,
)
from k8s_autopilot.config.paths import (
    CONFIG_PATH,
    DATA_DIR,
    ENV_PREFIX,
    GLOBAL_ENV_PATH,
    SESSIONS_DB_PATH,
    STATE_DIR,
    PathState,
    classify_path,
    ensure_agent_dir,
    ensure_data_dir,
    ensure_state_dir,
    find_project_root,
    project_mcp_paths,
    upsert_env_vars,
    user_agent_md,
    user_agents_dir,
    user_skills_dir,
)
from k8s_autopilot.config.settings import (
    Settings,
    get_settings,
    parse_shell_allow_list,
    reload_settings,
    resolve_env_var,
)


# ── Path Tests ───────────────────────────────────────────

def test_constants_and_paths():
    assert ENV_PREFIX == "K8S_AUTOPILOT_"
    assert DATA_DIR == Path.home() / ".k8s_autopilot"
    assert STATE_DIR == DATA_DIR / ".state"
    assert CONFIG_PATH == DATA_DIR / "config.toml"
    assert GLOBAL_ENV_PATH == DATA_DIR / ".env"
    assert SESSIONS_DB_PATH == STATE_DIR / "sessions.db"


def test_directory_helpers(tmp_path: Path):
    assert user_skills_dir("custom").name == "skills"
    assert user_agents_dir("custom").name == "agents"
    assert user_agent_md("custom").name == "AGENTS.md"

    mcp_paths = project_mcp_paths(tmp_path)
    assert len(mcp_paths) == 4
    assert mcp_paths[0] == tmp_path / ".mcp.json"


def test_find_project_root_helm(tmp_path: Path):
    project_dir = tmp_path / "my-helm-app"
    project_dir.mkdir()
    (project_dir / "Chart.yaml").write_text("name: test-chart\n")

    sub_dir = project_dir / "templates" / "sub"
    sub_dir.mkdir(parents=True)

    found = find_project_root(sub_dir)
    assert found == project_dir


def test_find_project_root_k8s_manifests(tmp_path: Path):
    project_dir = tmp_path / "k8s-infra"
    project_dir.mkdir()
    (project_dir / "kustomization.yaml").write_text("resources:\n  - app.yaml\n")

    sub_dir = project_dir / "overlays" / "prod"
    sub_dir.mkdir(parents=True)

    found = find_project_root(sub_dir)
    assert found == project_dir


def test_find_project_root_argocd(tmp_path: Path):
    project_dir = tmp_path / "gitops-repo"
    project_dir.mkdir()
    (project_dir / ".argocd").mkdir()

    sub_dir = project_dir / "apps"
    sub_dir.mkdir()

    found = find_project_root(sub_dir)
    assert found == project_dir


def test_classify_path(tmp_path: Path):
    existing_file = tmp_path / "file.txt"
    existing_file.write_text("hello")
    assert classify_path(existing_file) == PathState.EXISTS

    non_existing = tmp_path / "non_existing.txt"
    assert classify_path(non_existing) == PathState.MISSING


def test_upsert_env_vars(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING_KEY=old_val\n# A comment\nOTHER=keep\n")

    success = upsert_env_vars(
        {"EXISTING_KEY": "new_val", "NEW_KEY": "created_val"},
        env_path=env_file,
    )
    assert success
    content = env_file.read_text()
    assert "EXISTING_KEY=new_val" in content
    assert "OTHER=keep" in content
    assert "NEW_KEY=created_val" in content


# ── Settings Tests ───────────────────────────────────────

def test_settings_defaults():
    s = Settings()
    assert s.model is None
    assert s.approval_mode == "manual"
    assert s.kube_namespace == "default"
    assert s.mcp_timeout == 30
    assert not s.debug


def test_parse_shell_allow_list():
    assert parse_shell_allow_list(None) is None
    assert parse_shell_allow_list("") == []
    assert parse_shell_allow_list("  ") == []
    assert parse_shell_allow_list("kubectl get, helm list , git status") == [
        "kubectl get",
        "helm list",
        "git status",
    ]


def test_settings_from_env_with_prefix(monkeypatch):
    monkeypatch.setenv("K8S_AUTOPILOT_MODEL", "claude-3-7-sonnet")
    monkeypatch.setenv("K8S_AUTOPILOT_APPROVAL_MODE", "manual")
    monkeypatch.setenv("K8S_AUTOPILOT_KUBE_NAMESPACE", "talkops-prod")
    monkeypatch.setenv("K8S_AUTOPILOT_DEBUG", "true")
    monkeypatch.setenv("K8S_AUTOPILOT_SHELL_ALLOW_LIST", "kubectl get,helm list")
    monkeypatch.setenv("K8S_AUTOPILOT_MCP_TIMEOUT", "45")

    s = Settings.from_env()
    assert s.model == "claude-3-7-sonnet"
    assert s.approval_mode == "manual"
    assert s.kube_namespace == "talkops-prod"
    assert s.debug is True
    assert s.shell_allow_list == ["kubectl get", "helm list"]
    assert s.mcp_timeout == 45


def test_settings_from_env_fallbacks(monkeypatch):
    monkeypatch.delenv("K8S_AUTOPILOT_KUBECONFIG", raising=False)
    monkeypatch.delenv("K8S_AUTOPILOT_GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("KUBECONFIG", "/custom/kube/config")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-123")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key-456")

    s = Settings.from_env()
    assert s.kubeconfig == "/custom/kube/config"
    assert s.openai_api_key == "sk-test-key-123"
    assert s.google_api_key == "gemini-test-key-456"


def test_get_and_reload_settings(monkeypatch):
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2

    monkeypatch.setenv("K8S_AUTOPILOT_LOG_LEVEL", "DEBUG")
    reloaded = reload_settings()
    assert reloaded.log_level == "DEBUG"


# ── Manifest Tests ───────────────────────────────────────

def test_manifest_options():
    opts = get_config_options()
    assert len(opts) > 0

    keys = option_keys()
    assert "models.name" in keys
    assert "security.approval_mode" in keys
    assert "k8s.kubeconfig" in keys
    assert "credentials.openai" in keys
    assert "mcp.timeout" in keys

    model_opt = get_option("models.name")
    assert model_opt is not None
    assert model_opt.kind == OptionKind.STR
    assert model_opt.type_label == "str"
    assert model_opt.db_key == "MODEL"
    assert model_opt.default == "gemini-3.7-flash"


def test_manifest_option_by_db_key():
    opt = get_option_by_db_key("MODEL")
    assert opt is not None
    assert opt.key == "models.name"

    opt_secret = get_option_by_db_key("OPENAI_API_KEY")
    assert opt_secret is not None
    assert opt_secret.redacted is True


def test_manifest_groups():
    groups = iter_groups()
    assert "Credentials" in groups
    assert "Models" in groups
    assert "Kubernetes" in groups
    assert "Security" in groups


# ── Type Coercion Tests ──────────────────────────────────

def test_coerce_bool():
    for truthy in ("true", "1", "yes", "on", "TRUE", "Yes"):
        assert coerce_str_value(OptionKind.BOOL, truthy) is True
    for falsy in ("false", "0", "no", "off", "FALSE"):
        assert coerce_str_value(OptionKind.BOOL, falsy) is False
    assert coerce_str_value(OptionKind.BOOL, "maybe") is None


def test_coerce_int():
    assert coerce_str_value(OptionKind.INT, "42") == 42
    assert coerce_str_value(OptionKind.INT, "not_a_number") is None


def test_coerce_float():
    assert coerce_str_value(OptionKind.FLOAT, "3.14") == 3.14
    assert coerce_str_value(OptionKind.FLOAT, "not_a_float") is None


def test_coerce_shell_list():
    result = coerce_str_value(OptionKind.SHELL_LIST, "kubectl get, helm list , git status")
    assert result == ["kubectl get", "helm list", "git status"]


def test_coerce_path():
    result = coerce_str_value(OptionKind.PATH, "~/kubeconfig")
    assert isinstance(result, Path)
    assert str(result).startswith("/")


def test_coerce_json():
    assert coerce_str_value(OptionKind.JSON, '{"key": "value"}') == {"key": "value"}
    assert coerce_str_value(OptionKind.JSON, 'not valid json') is None


def test_serialize_typed_value():
    assert serialize_typed_value(OptionKind.BOOL, True) == "true"
    assert serialize_typed_value(OptionKind.BOOL, False) == "false"
    assert serialize_typed_value(OptionKind.INT, 42) == "42"
    assert serialize_typed_value(OptionKind.SHELL_LIST, ["a", "b"]) == "a,b"
    assert serialize_typed_value(OptionKind.JSON, {"k": "v"}) == '{"k": "v"}'
    assert serialize_typed_value(OptionKind.STR, None) == ""


# ── Env Var Resolution Tests ─────────────────────────────

def test_resolve_from_env_prefixed(monkeypatch):
    opt = ConfigOption(
        key="test.key",
        db_key="TEST_KEY",
        group="Test",
        summary="Test",
        kind=OptionKind.STR,
    )
    monkeypatch.setenv("K8S_AUTOPILOT_TEST_KEY", "prefixed_value")
    result = resolve_from_env(opt)
    assert result is not None
    assert result[0] == "prefixed_value"
    assert "K8S_AUTOPILOT_TEST_KEY" in result[1]


def test_resolve_from_env_unprefixed(monkeypatch):
    opt = ConfigOption(
        key="test.key2",
        db_key="TEST_KEY_2",
        group="Test",
        summary="Test",
        kind=OptionKind.INT,
    )
    monkeypatch.delenv("K8S_AUTOPILOT_TEST_KEY_2", raising=False)
    monkeypatch.setenv("TEST_KEY_2", "99")
    result = resolve_from_env(opt)
    assert result is not None
    assert result[0] == 99


def test_resolve_from_env_missing(monkeypatch):
    opt = ConfigOption(
        key="test.missing",
        db_key="TOTALLY_MISSING_KEY",
        group="Test",
        summary="Test",
        kind=OptionKind.STR,
    )
    monkeypatch.delenv("K8S_AUTOPILOT_TOTALLY_MISSING_KEY", raising=False)
    monkeypatch.delenv("TOTALLY_MISSING_KEY", raising=False)
    result = resolve_from_env(opt)
    assert result is None
