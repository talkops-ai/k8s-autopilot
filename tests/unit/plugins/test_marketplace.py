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
