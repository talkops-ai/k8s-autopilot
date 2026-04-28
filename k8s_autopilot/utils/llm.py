"""
LLM model factory for K8s Autopilot Agent.

Provides a single entry-point to instantiate any LLM tier from a config dict
produced by ``Config.llm_config``, ``Config.llm_higher_config``, etc.
"""


from typing import Any, Dict

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel


def create_model(llm_config: Dict[str, Any]) -> BaseChatModel:
    """
    Create a chat model instance from a config dict.

    The ``provider`` key (kept for backward-compat in ``Config``) is stripped
    automatically because ``init_chat_model()`` does not accept it.

    Args:
        llm_config: Config dict from any ``Config.llm_*_config`` property.

    Returns:
        A ready-to-use ``BaseChatModel`` instance.
    """
    kwargs = {k: v for k, v in llm_config.items() if k != "provider"}
    return init_chat_model(**kwargs)


# ── Convenience aliases (drop-in replacements for existing call-sites) ────

initialize_llm_model = create_model
initialize_llm_higher = create_model
initialize_llm_deepagent = create_model
