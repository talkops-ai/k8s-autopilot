"""Unit tests for marketplace parsing, source validation, and credential redaction."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from k8s_autopilot.plugins.marketplace import (
    MarketplaceError,
    load_marketplace,
    parse_marketplace_source,
    redact_marketplace_source,
    redact_urls_in_text,
)
from k8s_autopilot.plugins.models import (
    LocalMarketplaceSource,
    RepositoryMarketplaceSource,
    UrlMarketplaceSource,
)


def test_parse_marketplace_source() -> None:
    """Test parsing various marketplace source formats."""
    # GitHub repo
    gh = parse_marketplace_source("talkops-ai/k8s-plugins")
    assert isinstance(gh, RepositoryMarketplaceSource)
    assert gh.source_type == "github"
    assert gh.value == "talkops-ai/k8s-plugins"
    assert gh.ref is None

    # GitHub with ref
    gh_ref = parse_marketplace_source("talkops-ai/k8s-plugins#v2.0.0")
    assert isinstance(gh_ref, RepositoryMarketplaceSource)
    assert gh_ref.ref == "v2.0.0"

    # HTTPS URL
    url = parse_marketplace_source("https://registry.talkops.ai/marketplace.json")
    assert isinstance(url, UrlMarketplaceSource)
    assert url.value == "https://registry.talkops.ai/marketplace.json"

    # Insecure HTTP should fail
    with pytest.raises(MarketplaceError, match="must use https"):
        parse_marketplace_source("http://insecure.com/marketplace.json")


def test_redact_url_credentials() -> None:
    """Test redacting tokens and credentials from URLs and log text."""
    redacted = redact_marketplace_source("https://user:secrettoken@github.com/org/repo.git")
    assert "secrettoken" not in redacted
    assert "***" in redacted

    text = "Failed to clone https://oauth2:ghp_123456789@github.com/org/repo.git during install"
    redacted_text = redact_urls_in_text(text)
    assert "ghp_123456789" not in redacted_text


def test_load_marketplace_valid(tmp_path: Path) -> None:
    """Test loading a valid marketplace.json catalog."""
    manifest_file = tmp_path / "marketplace.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "community-marketplace",
                "metadata": {"pluginRoot": "./plugins"},
                "plugins": [
                    {
                        "name": "prometheus-analyzer",
                        "displayName": "Prometheus Analyzer",
                        "description": "Analyze Prometheus metrics",
                        "source": "./plugins/prometheus-analyzer",
                        "author": {"name": "TalkOps"},
                    },
                    {
                        "name": "gitops-syncer",
                        "source": {
                            "source": "github",
                            "repo": "talkops-ai/gitops-syncer",
                            "ref": "main",
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    marketplace = load_marketplace(tmp_path)
    assert marketplace.name == "community-marketplace"
    assert len(marketplace.plugins) == 2
    assert marketplace.plugins[0].name == "prometheus-analyzer"
    assert marketplace.plugins[0].display_name == "Prometheus Analyzer"
    assert marketplace.plugins[1].name == "gitops-syncer"


def test_redact_auth_headers_in_text() -> None:
    """Test redacting basic and bearer auth headers."""
    raw = "Git failed: -c http.https://github.com/.extraHeader=AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46c2VjcmV0"
    redacted = redact_urls_in_text(raw)
    assert "eC1hY2Nlc3MtdG9rZW46c2VjcmV0" not in redacted
    assert "AUTHORIZATION: [REDACTED]" in redacted


def test_clone_repository_with_github_token(monkeypatch, tmp_path):
    """Test that GITHUB_PERSONAL_ACCESS_TOKEN injects the extraHeader auth arg."""
    from k8s_autopilot.plugins.marketplace import _clone_repository_to_cache

    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_secrettoken123")
    monkeypatch.setenv("PLUGIN_CACHE_DIR", str(tmp_path / "cache"))

    calls = []

    def mock_run_git(args: list[str]) -> None:
        calls.append(args)
        target = Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("k8s_autopilot.plugins.marketplace._run_git", mock_run_git)

    source = RepositoryMarketplaceSource(source_type="github", value="talkops-ai/private-tools")
    res = _clone_repository_to_cache(source, "https://github.com/talkops-ai/private-tools.git", cache_key="test-key")
    assert res.exists()
    assert len(calls) == 1
    assert any("http.https://github.com/.extraHeader=AUTHORIZATION: basic" in arg for arg in calls[0])


def test_clone_repository_fallback_to_unauthenticated_on_auth_failure(monkeypatch, tmp_path):
    """Test that if authenticated clone fails with a bad token, it retries unauthenticated."""
    from k8s_autopilot.plugins.marketplace import _clone_repository_to_cache

    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "expired_token")
    monkeypatch.setenv("PLUGIN_CACHE_DIR", str(tmp_path / "cache"))

    calls = []

    def mock_run_git(args: list[str]) -> None:
        calls.append(args)
        if len(calls) == 1:
            raise MarketplaceError("fatal: could not read Username for 'https://github.com': terminal prompts disabled")
        target = Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("k8s_autopilot.plugins.marketplace._run_git", mock_run_git)

    source = RepositoryMarketplaceSource(source_type="github", value="talkops-ai/public-tools")
    res = _clone_repository_to_cache(source, "https://github.com/talkops-ai/public-tools.git", cache_key="test-fallback")
    assert res.exists()
    assert len(calls) == 2
    assert any("http.https://github.com/.extraHeader=AUTHORIZATION: basic" in arg for arg in calls[0])
    assert not any("http.https://github.com/.extraHeader=" in arg for arg in calls[1])


def test_clone_repository_missing_token_informative_error(monkeypatch, tmp_path):
    """Test that when token is missing and clone fails due to auth/missing repo, a clear error is raised."""
    from k8s_autopilot.plugins.marketplace import _clone_repository_to_cache

    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("PLUGIN_CACHE_DIR", str(tmp_path / "cache"))

    def mock_run_git(args: list[str]) -> None:
        raise MarketplaceError("fatal: could not read Username for 'https://github.com': terminal prompts disabled")

    monkeypatch.setattr("k8s_autopilot.plugins.marketplace._run_git", mock_run_git)

    source = RepositoryMarketplaceSource(source_type="github", value="talkops-ai/private-repo")
    with pytest.raises(MarketplaceError, match="If this is a private repository, please configure your GITHUB_PERSONAL_ACCESS_TOKEN in Settings"):
        _clone_repository_to_cache(source, "https://github.com/talkops-ai/private-repo.git", cache_key="test-missing-token")
