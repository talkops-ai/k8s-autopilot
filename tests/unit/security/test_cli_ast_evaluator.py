"""Unit tests for CLI AST Safety Evaluator (bashlex integration)."""

from __future__ import annotations

import pytest

from k8s_autopilot.security.cli_ast_evaluator import evaluate_cli_safety


def test_readonly_commands_tier1():
    """Verify inspection and read-only commands resolve to Tier 1."""
    readonly_cmds = [
        "kubectl get pods -n kube-system",
        "kubectl describe deployment coredns",
        "helm list -A",
        "helm status ingress-nginx",
        "git status",
        "git diff main",
        "cat /etc/hosts",
        "grep -r 'error' /var/log",
        "ls -la /tmp",
        "pwd",
        "kubectl cluster-info",
        "kubectl auth can-i create pods",
    ]
    for cmd in readonly_cmds:
        res = evaluate_cli_safety(cmd)
        assert res["tier"] == 1, f"Expected Tier 1 for {cmd}, got {res}"
        assert res["safe"] is True
        assert res["is_destructive"] is False


def test_destructive_commands_tier4():
    """Verify destructive verbs and flags resolve to Tier 4."""
    destructive_cmds = [
        "kubectl delete pod redis-master",
        "kubectl delete ns test-env",
        "helm uninstall prometheus-stack",
        "kubectl drain node-1 --ignore-daemonsets",
        "kubectl evict pod bad-pod",
        "kubectl taint nodes node1 key1=value1:NoSchedule",
        "rm -rf /tmp/data",
        "rm -f /tmp/lockfile",
        "kubectl delete pods --all",
        "helm uninstall vault --purge",
    ]
    for cmd in destructive_cmds:
        res = evaluate_cli_safety(cmd)
        assert res["tier"] == 4, f"Expected Tier 4 for {cmd}, got {res}"
        assert res["safe"] is False
        assert res["is_destructive"] is True


def test_composite_piped_destructive_commands():
    """Verify destructive commands inside xargs, subshells, and pipes resolve to Tier 4."""
    composite_cmds = [
        "kubectl get pods | grep Error | xargs kubectl delete",
        "sudo rm -rf /var/cache",
        "echo 'destroying' && helm uninstall cert-manager",
    ]
    for cmd in composite_cmds:
        res = evaluate_cli_safety(cmd)
        assert res["tier"] == 4, f"Expected Tier 4 for composite command {cmd}, got {res}"
        assert res["is_destructive"] is True


def test_mutating_non_destructive_commands_tier3():
    """Verify mutating commands (apply, scale, rollout) resolve to Tier 3."""
    mutating_cmds = [
        "kubectl apply -f deployment.yaml",
        "kubectl scale deployment nginx --replicas=3",
        "kubectl rollout restart deployment coredns",
        "helm install redis oci://registry/redis",
        "helm upgrade web-app ./chart",
    ]
    for cmd in mutating_cmds:
        res = evaluate_cli_safety(cmd)
        assert res["tier"] == 3, f"Expected Tier 3 for {cmd}, got {res}"
        assert res["is_destructive"] is False
        assert res["is_readonly"] is False


def test_syntax_error_fail_closed():
    """Verify unparseable syntax fails closed to Tier 4."""
    bad_cmd = "kubectl get pods | && > invalid syntax(("
    res = evaluate_cli_safety(bad_cmd)
    assert res["tier"] == 4
    assert res["is_destructive"] is True
    assert "fail-closed" in res["reason"]
