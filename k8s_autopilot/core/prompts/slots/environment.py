"""
Environment and working directory slot resolvers.

Provides slot resolvers for execution environment context:
  - ``{working_dir_section}`` — local vs sandbox working directory
  - ``{sandbox_description}`` — sandbox type description
  - ``{skills_path}`` — skills directory path

Adapted from dcode agent.py lines 892–934.
"""

from __future__ import annotations

from k8s_autopilot.core.prompts.resolver import PromptContext, PromptSlot


def _resolve_working_dir_section(ctx: PromptContext) -> str:
    """Resolve ``{working_dir_section}`` — local vs sandbox working dir.

    Follows dcode's working directory section pattern:
    - Sandbox mode: warn about remote environment, path constraints
    - Local mode: show filesystem path instructions
    """
    if ctx.sandbox_provider and ctx.sandbox_provider != "local":
        working_dir = ctx.working_dir or "/workspace"
        return (
            f"### Current Working Directory\n\n"
            f"You are operating in a **remote {ctx.sandbox_provider} sandbox** "
            f"at `{working_dir}`.\n\n"
            f"All code execution and file operations happen in this sandbox "
            f"environment.\n\n"
            f"**Important:**\n"
            f"- The application is running locally on the user's machine, but you "
            f"execute code remotely\n"
            f"- Use `{working_dir}` as your working directory for all operations\n"
            f"- **You do NOT have access to the user's local filesystem.** Paths "
            f"like `/Users/...`, `/home/<local-user>/...`, etc. do not "
            f"exist in this sandbox\n\n"
        )

    working_dir = ctx.working_dir or "."
    return (
        f"### Current Working Directory\n\n"
        f"The filesystem backend is currently operating in: `{working_dir}`\n\n"
        f"### File System and Paths\n\n"
        f"**IMPORTANT - Path Handling:**\n"
        f"- All file paths must be absolute paths (e.g., `{working_dir}/file.txt`)\n"
        f"- Use the working directory to construct absolute paths\n"
        f"- Never use relative paths - always construct full absolute paths\n\n"
    )


def _resolve_sandbox_description(ctx: PromptContext) -> str:
    """Resolve ``{sandbox_description}`` — human-readable sandbox type."""
    if ctx.sandbox_provider == "local":
        return "local shell execution (no sandbox)"
    return f"{ctx.sandbox_provider} remote sandbox"


def _resolve_skills_path(ctx: PromptContext) -> str:
    """Resolve ``{skills_path}`` — the skills directory path.

    Returns the domain-level parent (e.g., ``/skills/helm-operator/``) rather
    than the first individual skill leaf, so the prompt section reads correctly.
    """
    if ctx.skill_paths:
        path = ctx.skill_paths[0]
        # If the path already ends with / it's already a domain-level directory
        if path.endswith("/"):
            return path
        # Otherwise derive the domain-level parent (e.g., /skills/helm-operator/)
        # from a leaf path (e.g., /skills/helm-operator/helm-generator)
        parts = path.strip("/").split("/")
        if len(parts) > 2:
            # /skills/<domain>/<skill-leaf> → /skills/<domain>/
            return "/" + "/".join(parts[:2]) + "/"
        return path
    return "/skills/"


def get_environment_slots() -> list[PromptSlot]:
    """Return all environment/working-directory slot resolvers."""
    return [
        PromptSlot(name="working_dir_section", resolver=_resolve_working_dir_section),
        PromptSlot(name="sandbox_description", resolver=_resolve_sandbox_description),
        PromptSlot(name="skills_path", resolver=_resolve_skills_path),
    ]
