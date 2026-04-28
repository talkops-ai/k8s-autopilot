"""
Skill Writer Orchestrator Tool
------------------------------
This module drives the pipeline's virtual filesystem serialization
for Agent Skills using the full architecture planning output.
"""

import os
from pathlib import Path
from typing import Any, Dict

from langchain.tools import tool, ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from k8s_autopilot.core.state.base import HelmPlannerState
from k8s_autopilot.utils.logger import AgentLogger

from deepagents.backends.utils import create_file_data

from .skill_templates import (
    render_skill_md,
    render_execution_blueprint,
    render_values_schema,
    render_scaling_and_resources,
    render_dependencies_blueprint,
    render_security_blueprint,
    render_manifest_patterns,
    _slugify,
)

logger = AgentLogger("HELM_SKILL_WRITER")

def _build_files_dict(skill_dir: str, skill_md: str, refs: dict) -> dict:
    files_dict = {}
    files_dict[f"{skill_dir}/SKILL.md"] = create_file_data(skill_md)
    for filename, content in refs.items():
        files_dict[f"{skill_dir}/{filename}"] = create_file_data(content)
    return files_dict

def _sync_skills_to_disk(skill_dir: str, skill_md: str, refs: dict) -> None:
    """Write skill files to local disk for persistence and inspection."""
    if os.getenv("SKILL_WRITER_SYNC_DISK", "true").lower() == "false":
        return

    # skill_dir = "/skills/helm-operator/nginx-chart-generator"
    # -> local = "./skills/helm-operator/nginx-chart-generator"
    local_root = Path(os.getenv("AGENT_PROJECT_ROOT", ".")).resolve()
    local_skill_dir = local_root / skill_dir.lstrip("/")

    try:
        # Write SKILL.md
        local_skill_dir.mkdir(parents=True, exist_ok=True)
        (local_skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

        # Write reference files
        for filename, content in refs.items():
            file_path = local_skill_dir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        logger.info(
            f"Synced Helm skill to disk: {local_skill_dir}",
            extra={"file_count": 1 + len(refs), "disk_path": str(local_skill_dir)},
        )
    except OSError as e:
        logger.warning(f"Failed to sync Helm skill to disk: {e}")

@tool
async def write_chart_skills_tool(
    runtime: ToolRuntime[None, HelmPlannerState],
) -> Command:
    """
    Write Agent Skills directory for the parsed Kubernetes Architecture.
    
    Reads `handoff_data` from state which contains:
    - parsed_requirements
    - application_analysis
    - kubernetes_architecture
    - resource_estimation
    - scaling_strategy
    - dependencies
    
    Generates:
      /skills/helm-operator/{app_name}-chart-generator/SKILL.md
      /skills/helm-operator/{app_name}-chart-generator/references/execution-blueprint.md
      /skills/helm-operator/{app_name}-chart-generator/references/values-schema.md
      /skills/helm-operator/{app_name}-chart-generator/references/scaling-and-resources.md
      /skills/helm-operator/{app_name}-chart-generator/references/dependencies-blueprint.md
      /skills/helm-operator/{app_name}-chart-generator/references/security-blueprint.md
      /skills/helm-operator/{app_name}-chart-generator/references/manifest-patterns.md
      
    This directly allows the Helm Coordinator deep agent to skip 'helm-skill-builder' and
    proceed directly to 'helm-generator'.
    """
    handoff_data = dict(runtime.state.get("handoff_data", {}))
    
    parsed_reqs = handoff_data.get("parsed_requirements", {})
    app_analysis = handoff_data.get("application_analysis", {})
    k8s_arch = handoff_data.get("kubernetes_architecture", {})
    resource_est = handoff_data.get("resource_estimation", {})
    scaling_strat = handoff_data.get("scaling_strategy", {})
    
    # As explicitly requested: Do not mention any dependencies while writing the skill
    dependencies = {}
    
    app_name = parsed_reqs.get("app_name", "unknown")
    if app_name == "unknown":
        return Command(
            update={
                "messages": [ToolMessage(
                    content="Application name not found in requirements. 0 skills written.",
                    tool_call_id=runtime.tool_call_id,
                )]
            }
        )

    # Log data presence for diagnostics
    logger.info(
        f"Skill writer received data for {app_name}",
        extra={
            "has_parsed_reqs": bool(parsed_reqs),
            "has_app_analysis": bool(app_analysis),
            "has_k8s_arch": bool(k8s_arch),
            "has_resource_est": bool(resource_est),
            "has_scaling_strat": bool(scaling_strat),
            "has_dependencies": bool(dependencies),
            "k8s_arch_keys": list(k8s_arch.keys()) if k8s_arch else [],
            "parsed_reqs_keys": list(parsed_reqs.keys()) if parsed_reqs else [],
            "resource_est_keys": list(resource_est.keys()) if resource_est else [],
            "scaling_strat_keys": list(scaling_strat.keys()) if scaling_strat else [],
            "deps_keys": list(dependencies.keys()) if dependencies else [],
        },
    )

    app_slug = _slugify(app_name)
    skill_dir = f"/skills/helm-operator/{app_slug}-chart-generator"

    logger.info(f"Rendering Helm skill for {app_name} at {skill_dir}")

    try:
        skill_md = render_skill_md(
            app_name=app_name,
            parsed_requirements=parsed_reqs,
            application_analysis=app_analysis,
            kubernetes_architecture=k8s_arch,
            resource_estimation=resource_est,
            scaling_strategy=scaling_strat,
            dependencies=dependencies,
        )

        refs = {
            "references/execution-blueprint.md": render_execution_blueprint(
                app_name, k8s_arch,
            ),
            "references/values-schema.md": render_values_schema(
                parsed_reqs,
                resource_est,
                scaling_strat,
                application_analysis=app_analysis,
                kubernetes_architecture=k8s_arch,
                dependencies=dependencies,
            ),
            "references/scaling-and-resources.md": render_scaling_and_resources(
                scaling_strat, resource_est,
            ),
            "references/dependencies-blueprint.md": render_dependencies_blueprint(
                dependencies,
            ),
            "references/security-blueprint.md": render_security_blueprint(
                app_analysis, k8s_arch,
            ),
            "references/manifest-patterns.md": render_manifest_patterns(
                app_name, k8s_arch, app_analysis, dependencies,
            ),
        }

        # Log rendered content sizes for diagnostics
        for ref_name, ref_content in refs.items():
            logger.info(
                f"Rendered {ref_name}",
                extra={"size_bytes": len(ref_content)},
            )

        rendered_files = _build_files_dict(skill_dir, skill_md, refs)
        _sync_skills_to_disk(skill_dir, skill_md, refs)

        existing_files = dict(runtime.state.get("files", {}))
        existing_files.update(rendered_files)

        handoff_data["skills_written"] = [
            {
                "app": app_name,
                "path": skill_dir,
                "files": list(rendered_files.keys())
            }
        ]

        # The message MUST contain "Skills written for" to trigger the skip
        return Command(
            update={
                "files": existing_files,
                "handoff_data": handoff_data,
                "messages": [ToolMessage(
                    content=f"Skills written for {app_name} Helm Chart.",
                    tool_call_id=runtime.tool_call_id,
                )],
            }
        )
    except Exception as e:
        logger.error(f"Failed to render templates for {app_name}: {e}")
        return Command(
            update={
                "messages": [ToolMessage(
                    content=f"Failed to write skills for {app_name}: {str(e)}",
                    tool_call_id=runtime.tool_call_id,
                )]
            }
        )
