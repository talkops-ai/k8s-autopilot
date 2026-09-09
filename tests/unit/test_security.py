"""Unit tests for K8s Autopilot Security Module (Phase 16)."""

import ipaddress
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from k8s_autopilot.security.approval_mode import (
    ApprovalMode,
    approval_mode_key,
    approval_mode_payload,
    awrite_approval_mode,
    coerce_approval_mode,
    has_auto_mode_notice,
    has_yolo_acknowledgement,
    next_approval_mode,
    read_approval_mode_from_store,
    save_auto_mode_notice,
    save_yolo_acknowledgement,
)
from k8s_autopilot.security.approval_mode_source import (
    ApprovalPolicyResolver,
    _DecidedMode,
    _LiveLookup,
    _aresolve_approval_mode,
    _resolve_approval_mode,
)
from k8s_autopilot.security.shell_safety import (
    classify_command,
    contains_dangerous_patterns,
    is_shell_command_allowed,
)
from k8s_autopilot.security.unicode_security import (
    UnicodeIssue,
    check_url_safety,
    detect_dangerous_unicode,
    format_warning_detail,
    iter_string_values,
    looks_like_url_key,
    render_with_unicode_markers,
    sanitize_control_chars,
    strip_dangerous_unicode,
    summarize_issues,
)
from k8s_autopilot.security.url_validation import (
    _UrlValidationError,
    _is_blocked_ip,
    _pinned_dns,
    _validate_url,
)


# ── Approval Mode Tests ──────────────────────────────────

def test_approval_mode_enum():
    assert ApprovalMode.AUTO == "auto"
    assert ApprovalMode.MANUAL == "manual"
    assert ApprovalMode.YOLO == "yolo"


def test_coerce_approval_mode():
    assert coerce_approval_mode(None) == ApprovalMode.MANUAL
    assert coerce_approval_mode(True) == ApprovalMode.YOLO
    assert coerce_approval_mode(False) == ApprovalMode.MANUAL
    assert coerce_approval_mode("manual") == ApprovalMode.MANUAL
    assert coerce_approval_mode("MANUAL") == ApprovalMode.MANUAL
    assert coerce_approval_mode("yolo") == ApprovalMode.YOLO
    assert coerce_approval_mode("auto") == ApprovalMode.AUTO
    assert coerce_approval_mode("invalid_mode") == ApprovalMode.MANUAL


def test_next_approval_mode_cycle():
    assert next_approval_mode(ApprovalMode.MANUAL) == ApprovalMode.AUTO
    assert next_approval_mode(ApprovalMode.AUTO) == ApprovalMode.YOLO
    assert next_approval_mode(ApprovalMode.YOLO) == ApprovalMode.MANUAL

    # Without auto eligible
    assert next_approval_mode(ApprovalMode.MANUAL, auto_eligible=False) == ApprovalMode.YOLO
    # Without yolo switcher
    assert next_approval_mode(ApprovalMode.AUTO, yolo_switcher_enabled=False) == ApprovalMode.MANUAL


def test_approval_mode_payload():
    assert approval_mode_payload(mode="auto") == {"mode": "auto"}
    assert approval_mode_payload(auto_approve=True) == {"mode": "yolo"}
    assert approval_mode_payload(auto_approve=False) == {"mode": "manual"}

    with pytest.raises(ValueError):
        approval_mode_payload(mode="auto", auto_approve=True)


def test_approval_mode_key():
    k1 = approval_mode_key("thread-123")
    k2 = approval_mode_key("thread-123")
    k3 = approval_mode_key("thread-456")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64


def test_yolo_acknowledgement_and_auto_notice(tmp_path: Path):
    test_ack_path = tmp_path / "approval.json"
    assert not has_yolo_acknowledgement(test_ack_path)
    assert not has_auto_mode_notice(test_ack_path)

    save_yolo_acknowledgement(test_ack_path)
    assert has_yolo_acknowledgement(test_ack_path)

    save_auto_mode_notice(test_ack_path)
    assert has_auto_mode_notice(test_ack_path)


# ── Approval Policy Resolver Tests ───────────────────────

def test_approval_policy_resolver_dict_context():
    # None context fails closed to manual
    src_none = ApprovalPolicyResolver.resolve_source(None)
    assert isinstance(src_none, _DecidedMode)
    assert src_none.mode == ApprovalMode.MANUAL

    # Empty context fails closed to manual
    src_empty = ApprovalPolicyResolver.resolve_source({})
    assert isinstance(src_empty, _DecidedMode)
    assert src_empty.mode == ApprovalMode.MANUAL

    # Typed mode
    src = ApprovalPolicyResolver.resolve_source({"approval_mode": "manual"})
    assert isinstance(src, _DecidedMode)
    assert src.mode == ApprovalMode.MANUAL

    # Legacy auto_approve
    src_yolo = ApprovalPolicyResolver.resolve_source({"auto_approve": True})
    assert isinstance(src_yolo, _DecidedMode)
    assert src_yolo.mode == ApprovalMode.YOLO

    # Store lookup
    t_id = "thread-xyz"
    key = approval_mode_key(t_id)
    src_store = ApprovalPolicyResolver.resolve_source({"approval_mode_key": key, "thread_id": t_id})
    assert isinstance(src_store, _LiveLookup)
    assert src_store.key == key


def test_approval_mode_store_resolution():
    mock_store = MagicMock()
    mock_store.get.return_value = {"value": {"mode": "manual"}}

    t_id = "test-thread"
    key = approval_mode_key(t_id)
    resolved = _resolve_approval_mode({"approval_mode_key": key, "thread_id": t_id}, mock_store)
    assert resolved == ApprovalMode.MANUAL

    # Unavailable store entry fails closed to manual
    mock_store.get.return_value = None
    resolved_fallback = _resolve_approval_mode({"approval_mode_key": key, "thread_id": t_id}, mock_store)
    assert resolved_fallback == ApprovalMode.MANUAL


@pytest.mark.asyncio
async def test_aresolve_approval_mode():
    mock_store = MagicMock()
    mock_store.aget = AsyncMock(return_value={"value": {"mode": "yolo"}})

    t_id = "test-thread"
    key = approval_mode_key(t_id)
    resolved = await _aresolve_approval_mode({"approval_mode_key": key, "thread_id": t_id}, mock_store)
    assert resolved == ApprovalMode.YOLO


@pytest.mark.asyncio
async def test_awrite_approval_mode():
    mock_agent = MagicMock()
    mock_agent.aput_store_item = AsyncMock()

    key = await awrite_approval_mode(mock_agent, "thread-999", mode="manual")
    assert key is not None
    assert mock_agent.aput_store_item.called
    call_args = mock_agent.aput_store_item.call_args[0]
    assert call_args[1] == key
    assert call_args[2] == {"mode": "manual"}


# ── Command Classification Tests ─────────────────────────

def test_classify_safe_commands():
    assert classify_command("kubectl get pods -n default") == "safe"
    assert classify_command("kubectl describe service/my-svc") == "safe"
    assert classify_command("kubectl logs -f deployment/api") == "safe"
    assert classify_command("helm list -A") == "safe"
    assert classify_command("helm status release-prod") == "safe"
    assert classify_command("argocd app get frontend") == "safe"
    assert classify_command("git status") == "safe"
    assert classify_command("cat values.yaml") == "safe"
    assert classify_command("grep -rn 'apiVersion' .") == "safe"
    assert classify_command("") == "safe"


def test_classify_dangerous_commands():
    assert classify_command("kubectl delete pods --all -n default") == "dangerous"
    assert classify_command("kubectl delete namespace staging") == "dangerous"
    assert classify_command("kubectl delete ns test") == "dangerous"
    assert classify_command("helm uninstall my-release") == "dangerous"
    assert classify_command("helm delete old-release") == "dangerous"
    assert classify_command("kubectl drain node-1 --force") == "dangerous"
    assert classify_command("kubectl cordon node-2") == "dangerous"
    assert classify_command("kubectl taint node node-1 key=value:NoSchedule") == "dangerous"
    assert classify_command("argocd app delete guestbook") == "dangerous"
    assert classify_command("kubectl apply -f bad.yaml -n kube-system") == "dangerous"
    assert classify_command("kubectl apply -f bad.yaml --namespace kube-system") == "dangerous"
    assert classify_command("rm -rf /") == "dangerous"


def test_classify_production_namespace_mutations():
    assert classify_command("kubectl apply -f deployment.yaml -n production") == "dangerous"
    assert classify_command("kubectl patch svc web -n prod-us-east") == "dangerous"
    assert classify_command("kubectl apply -f manifest.yaml --namespace=live") == "dangerous"
    # Safe read commands in production should still be safe
    assert classify_command("kubectl get pods -n production") == "safe"


def test_classify_ambiguous_commands():
    assert classify_command("kubectl apply -f deployment.yaml -n staging") == "ambiguous"
    assert classify_command("helm upgrade my-chart ./chart -n dev") == "ambiguous"
    assert classify_command("kubectl rollout restart deployment/api -n test") == "ambiguous"


# ── Shell Allow List Tests ───────────────────────────────

def test_dangerous_shell_patterns():
    assert contains_dangerous_patterns("echo $(whoami)")
    assert contains_dangerous_patterns("echo `id`")
    assert contains_dangerous_patterns("cat file > out.txt")
    assert contains_dangerous_patterns("cmd &")
    assert contains_dangerous_patterns("echo $SECRET")
    assert not contains_dangerous_patterns("kubectl get pods -n default")


def test_is_shell_command_allowed():
    allow_list = ["kubectl get", "helm list", "git status"]
    assert is_shell_command_allowed("kubectl get pods", allow_list)
    assert is_shell_command_allowed("helm list -A", allow_list)
    assert is_shell_command_allowed("git status", allow_list)
    assert not is_shell_command_allowed("kubectl delete pods", allow_list)
    assert not is_shell_command_allowed("rm -rf /", allow_list)
    # Blocked due to substitution even if prefix matches
    assert not is_shell_command_allowed("kubectl get pods $(rm -rf /)", allow_list)

    # Compound allowed vs disallowed
    assert is_shell_command_allowed("kubectl get pods && helm list", allow_list)
    assert not is_shell_command_allowed("kubectl get pods && kubectl delete pods", allow_list)


# ── Unicode Security Tests ───────────────────────────────

def test_detect_dangerous_unicode():
    text_with_bidi = "hello\u202Eworld"
    issues = detect_dangerous_unicode(text_with_bidi)
    assert len(issues) == 1
    assert issues[0].codepoint == "U+202E"

    clean_text = "clean ascii text"
    assert len(detect_dangerous_unicode(clean_text)) == 0


def test_strip_dangerous_unicode():
    text = "hello\u200Bworld\uFEFF"
    assert strip_dangerous_unicode(text) == "helloworld"


def test_sanitize_control_chars():
    text = "line1\x00\x01\nline2"
    sanitized = sanitize_control_chars(text, keep_newlines=True)
    assert "line1\nline2" == sanitized


def test_render_with_unicode_markers():
    text = "hello\u202Eworld"
    rendered = render_with_unicode_markers(text)
    assert "<U+202E RIGHT-TO-LEFT OVERRIDE>" in rendered


def test_summarize_issues():
    issues = [
        UnicodeIssue(position=0, character="\u200B", codepoint="U+200B", name="ZERO WIDTH SPACE"),
        UnicodeIssue(position=1, character="\u200C", codepoint="U+200C", name="ZERO WIDTH NON-JOINER"),
    ]
    summary = summarize_issues(issues)
    assert "U+200B ZERO WIDTH SPACE" in summary
    assert "U+200C ZERO WIDTH NON-JOINER" in summary


def test_format_warning_detail():
    warnings = ("Warning 1", "Warning 2", "Warning 3")
    formatted = format_warning_detail(warnings, max_shown=2)
    assert "Warning 1; Warning 2; +1 more" == formatted


def test_check_url_safety():
    # Safe public URL
    safe_res = check_url_safety("https://github.com/talkops-ai/k8s-autopilot")
    assert safe_res.safe

    # URL with bidi
    bidi_url = "https://example.com/\u202Ebad"
    bidi_res = check_url_safety(bidi_url)
    assert not bidi_res.safe
    assert len(bidi_res.issues) > 0

    # Confusable mixed script domain: replace Latin 'a' with Cyrillic 'а' (\u0430)
    confusable_url = "https://p\u0430ypal.com"
    confusable_res = check_url_safety(confusable_url)
    assert not confusable_res.safe


def test_looks_like_url_key():
    assert looks_like_url_key("api_url")
    assert looks_like_url_key("endpoint")
    assert looks_like_url_key("item[0].href")
    assert not looks_like_url_key("username")


def test_iter_string_values():
    payload = {
        "command": "kubectl get pods",
        "nested": {"url": "https://example.com"},
        "items": ["val1", {"deep": "val2"}],
    }
    extracted = dict(iter_string_values(payload))
    assert extracted["command"] == "kubectl get pods"
    assert extracted["nested.url"] == "https://example.com"
    assert extracted["items[0]"] == "val1"
    assert extracted["items[1].deep"] == "val2"


# ── URL Validation & SSRF Tests ──────────────────────────

def test_is_blocked_ip():
    assert _is_blocked_ip(ipaddress.ip_address("127.0.0.1"))
    assert _is_blocked_ip(ipaddress.ip_address("10.0.0.1"))
    assert _is_blocked_ip(ipaddress.ip_address("172.16.0.1"))
    assert _is_blocked_ip(ipaddress.ip_address("192.168.1.1"))
    assert _is_blocked_ip(ipaddress.ip_address("169.254.169.254"))  # AWS metadata
    assert _is_blocked_ip(ipaddress.ip_address("::1"))
    assert not _is_blocked_ip(ipaddress.ip_address("8.8.8.8"))
    assert not _is_blocked_ip(ipaddress.ip_address("1.1.1.1"))


def test_validate_url_schemes():
    with pytest.raises(_UrlValidationError, match="scheme not allowed"):
        _validate_url("ftp://example.com/file")

    with pytest.raises(_UrlValidationError, match="scheme not allowed"):
        _validate_url("file:///etc/passwd")


def test_pinned_dns():
    with _pinned_dns("example.com", ["93.184.216.34"]):
        pass  # Ensure context manager enters and exits cleanly
