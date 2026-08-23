"""
Dynamic System Prompt Resolution for K8s Autopilot Deep Agents.

Provides a template-based prompt resolution system following the dcode pattern:
  - Prompts live as ``.md`` template files with ``{slot_name}`` placeholders
  - ``PromptResolver`` loads templates and resolves slots at runtime
  - ``PromptSlot`` instances are pluggable resolvers (mode, model, env, etc.)
  - ``PromptContext`` carries runtime state available to all resolvers

Usage::

    from k8s_autopilot.core.prompts import (
        InteractionMode,
        PromptContext,
        PromptResolver,
        PromptSlot,
        create_default_resolver,
    )

    resolver = create_default_resolver(template_path)
    ctx = PromptContext(mode=InteractionMode.INTERACTIVE, model_name="o4-mini")
    prompt = resolver.resolve(ctx)
"""

from k8s_autopilot.core.prompts.resolver import (
    InteractionMode,
    PromptContext,
    PromptResolver,
    PromptSlot,
    create_default_resolver,
)

__all__ = [
    "InteractionMode",
    "PromptContext",
    "PromptResolver",
    "PromptSlot",
    "create_default_resolver",
]
