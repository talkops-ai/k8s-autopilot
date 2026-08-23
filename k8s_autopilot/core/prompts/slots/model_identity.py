"""
Model identity slot resolvers.

Provides the ``{model_identity_section}`` slot that injects the active
model name, provider, context window, and unsupported modality warnings
into the system prompt.

Adapted from dcode agent.py::build_model_identity_section() (lines 750–789).
"""

from __future__ import annotations

from k8s_autopilot.core.prompts.resolver import PromptContext, PromptSlot


def _resolve_model_identity_section(ctx: PromptContext) -> str:
    """Resolve ``{model_identity_section}`` — model name, provider, limits.

    Follows dcode's build_model_identity_section() pattern: a markdown
    block telling the agent what model it is, what its context window is,
    and what input modalities are unavailable.
    """
    if not ctx.model_name:
        return ""

    parts: list[str] = [
        "### Model Identity\n",
        f"You are running as model **{ctx.model_name}**",
    ]

    if ctx.model_provider:
        parts.append(f" (provider: {ctx.model_provider})")

    parts.append(".\n")

    if ctx.context_limit:
        parts.append(
            f"\nYour context window is **{ctx.context_limit:,}** tokens.\n"
        )

    # Unsupported modality warnings (dcode pattern — lines 775-789)
    if ctx.unsupported_modalities:
        items = ctx.unsupported_modalities
        if len(items) == 1:
            joined = items[0]
        elif len(items) == 2:
            joined = f"{items[0]} and {items[1]}"
        else:
            joined = ", ".join(items[:-1]) + f", and {items[-1]}"

        parts.append(
            f"\n{joined.capitalize()} input may not be available for this "
            "model. Do not attempt to read or process these content types.\n"
        )

    parts.append("\n")
    return "".join(parts)


def get_model_identity_slots() -> list[PromptSlot]:
    """Return model identity slot resolvers."""
    return [
        PromptSlot(
            name="model_identity_section",
            resolver=_resolve_model_identity_section,
        ),
    ]
