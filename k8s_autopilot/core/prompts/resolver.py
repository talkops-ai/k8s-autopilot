"""
Core prompt resolver — loads .md templates and interpolates dynamic slots.

Follows the dcode ``get_system_prompt()`` pattern:
  1. Load template from ``.md`` file via ``Path.read_text()``
  2. Resolve each ``{slot_name}`` via registered ``PromptSlot`` resolvers
  3. Validate no unreplaced placeholders remain (defense-in-depth)

The resolver is intentionally decoupled from any specific coordinator —
each domain (Helm, App, Observability, etc.) provides its own template
and can register domain-specific slots via ``register_slot()``.

Reference: dcode/code/agent.py::get_system_prompt() (lines 792–951)
"""

from __future__ import annotations

from k8s_autopilot.utils.logger import AgentLogger
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("PromptResolver")


# ---------------------------------------------------------------------------
# Interaction Mode
# ---------------------------------------------------------------------------

class InteractionMode(str, Enum):
    """Agent execution mode — drives prompt behavior differences.

    INTERACTIVE: A2A session with human operator monitoring in real-time.
    HEADLESS: Non-interactive CI/CD or batch execution — no human available.
    """

    INTERACTIVE = "interactive"
    HEADLESS = "headless"


# ---------------------------------------------------------------------------
# Prompt Context
# ---------------------------------------------------------------------------

@dataclass
class PromptContext:
    """Runtime context available to all slot resolvers.

    Carries everything a slot resolver might need to generate its content.
    Populated by the coordinator at ``build_agent()`` time and passed to
    ``PromptResolver.resolve()``.
    """

    mode: InteractionMode = InteractionMode.INTERACTIVE
    model_name: str = ""
    model_provider: str = ""
    sandbox_provider: str = "local"
    working_dir: str = ""
    skill_paths: list[str] = field(default_factory=list)
    config: Optional["Config"] = None

    # Model capability hints (populated by ModelCapabilityRegistry)
    context_limit: Optional[int] = None
    unsupported_modalities: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt Slot
# ---------------------------------------------------------------------------

@dataclass
class PromptSlot:
    """Named dynamic slot that resolves to a string at runtime.

    Each slot corresponds to a ``{slot_name}`` placeholder in the template.
    The ``resolver`` callable receives a ``PromptContext`` and returns the
    interpolated content.

    Example::

        slot = PromptSlot(
            name="mode_description",
            resolver=lambda ctx: "interactive A2A session" if ctx.mode == InteractionMode.INTERACTIVE else "headless mode",
        )
    """

    name: str
    resolver: Callable[[PromptContext], str]


# ---------------------------------------------------------------------------
# Prompt Resolver
# ---------------------------------------------------------------------------

class PromptResolver:
    """Resolves .md prompt templates with dynamic {slot} interpolation.

    Follows the dcode pattern (agent.py lines 936–951):
      1. Load template from .md file
      2. Replace each ``{slot_name}`` via registered PromptSlot resolvers
      3. Detect and warn on unreplaced placeholders (defense-in-depth)

    Thread-safe: resolver instances are stateless after construction
    (slots are registered once at build time).
    """

    def __init__(self, template_path: Optional[Path] = None) -> None:
        self._template_path = template_path
        self._slots: dict[str, PromptSlot] = {}

    @property
    def template_path(self) -> Optional[Path]:
        return self._template_path

    @template_path.setter
    def template_path(self, path: Path) -> None:
        self._template_path = path

    def register_slot(self, slot: PromptSlot) -> None:
        """Register a named slot resolver."""
        self._slots[slot.name] = slot

    def register_slots(self, slots: list[PromptSlot]) -> None:
        """Register multiple slot resolvers at once."""
        for slot in slots:
            self.register_slot(slot)

    @property
    def registered_slots(self) -> list[str]:
        """Return names of all registered slots (for debugging/testing)."""
        return list(self._slots.keys())

    def resolve(
        self,
        ctx: PromptContext,
        template_override: Optional[str] = None,
    ) -> str:
        """Resolve a prompt template with the given runtime context.

        Args:
            ctx: Runtime context carrying mode, model, environment info.
            template_override: If provided, use this string instead of
                loading from ``self._template_path``.

        Returns:
            The fully resolved prompt string.

        Raises:
            FileNotFoundError: If no template_override and the template
                file doesn't exist.
        """
        # Step 1: Load template
        if template_override is not None:
            template = template_override
        elif self._template_path and self._template_path.exists():
            template = self._template_path.read_text(encoding="utf-8")
        else:
            raise FileNotFoundError(
                f"Prompt template not found: {self._template_path}"
            )

        # Step 2: Resolve slots via string replacement (dcode pattern)
        result = template
        for name, slot in self._slots.items():
            placeholder = f"{{{name}}}"
            if placeholder in result:
                try:
                    resolved_value = slot.resolver(ctx)
                    result = result.replace(placeholder, resolved_value)
                except Exception as e:
                    logger.error(f"Failed to resolve prompt slot {name!r}: {e}", exc_info=True)
                    # Leave placeholder in place rather than crash
                    result = result.replace(
                        placeholder,
                        f"<!-- ERROR resolving {name}: {e} -->",
                    )

        # Step 3: Defense-in-depth — warn on unreplaced placeholders
        # (dcode agent.py line 947–949)
        # Filter out known literal documentation placeholders presented to the LLM
        doc_placeholders = {"{app}", "{repo}", "{branch}", "{chart_path}", "{chart_name}", "{errors}"}
        unreplaced = [
            p for p in re.findall(r"\{[a-z_]+\}", result)
            if p not in doc_placeholders
        ]
        if unreplaced:
            logger.warning(f"Prompt contains unreplaced placeholders: {unreplaced}")

        return result


# ---------------------------------------------------------------------------
# Factory — create a resolver with all built-in slots pre-registered
# ---------------------------------------------------------------------------

def create_default_resolver(
    template_path: Optional[Path] = None,
) -> PromptResolver:
    """Create a ``PromptResolver`` with all standard built-in slots.

    Built-in slots (from ``core/prompts/slots/``):
      - ``mode_description`` — interactive vs headless
      - ``interactive_preamble`` — mode-specific preamble
      - ``ambiguity_guidance`` — ask vs assume
      - ``todo_guidance`` — todo list management rules
      - ``model_identity_section`` — model name/provider/limits
      - ``working_dir_section`` — execution environment
      - ``sandbox_description`` — sandbox type
      - ``skills_path`` — skills directory path

    Args:
        template_path: Path to the ``.md`` template file.

    Returns:
        A fully configured ``PromptResolver`` instance.
    """
    from k8s_autopilot.core.prompts.slots.mode import get_mode_slots
    from k8s_autopilot.core.prompts.slots.model_identity import (
        get_model_identity_slots,
    )
    from k8s_autopilot.core.prompts.slots.environment import (
        get_environment_slots,
    )

    resolver = PromptResolver(template_path=template_path)

    # Register all built-in slot resolvers
    resolver.register_slots(get_mode_slots())
    resolver.register_slots(get_model_identity_slots())
    resolver.register_slots(get_environment_slots())

    return resolver
