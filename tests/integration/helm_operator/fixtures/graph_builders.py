import pytest
from k8s_autopilot.core.agents.helm_operator.coordinator import HelmOperatorCoordinator


def get_integration_coordinator(mock_config):
    coordinator = HelmOperatorCoordinator(config=mock_config)
    return coordinator
