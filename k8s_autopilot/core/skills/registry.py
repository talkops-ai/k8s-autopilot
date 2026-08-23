"""Central Skill Registry and Discovery for k8s-autopilot.

Dynamically scans, parses, and loads agent skills from the filesystem,
following the OpenWiki philosophy and the dcode/deepagents spec.
"""

import os
import re
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict
import threading

from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("SkillRegistry")


class SkillMetadata(TypedDict):
    """Metadata representing a discovered agent skill."""
    name: str
    """Unique identifier for the skill, parsed from frontmatter or folder name."""

    description: str
    """What the skill does. Used by LLM/middlewares for dynamic turn-based matching."""

    domain: str
    """The operator domain (e.g. 'helm-operator', 'observability', 'k8s-operator')."""

    path: str
    """Absolute physical path to the SKILL.md file on disk."""

    virtual_path: str
    """Virtual path of the skill directory in the deep agent virtual FS."""

    frontmatter: Dict[str, Any]
    """Parsed YAML frontmatter dictionary."""

    system_prompt: str
    """The markdown body instructions of the SKILL.md file."""


class SkillRegistry:
    """Registry that dynamically scans and parses skills from directories."""

    def __init__(self) -> None:
        self._skills: Dict[str, SkillMetadata] = {}
        self._project_skills_root: Optional[Path] = None
        self._lock = threading.Lock()
        self._is_discovered = False

    def _ensure_skills_symlinks(self) -> None:
        """Create symlinks under <root>/skills/ pointing to plugins/ skills directories.

        This ensures that the FilesystemBackend(root_dir='skills', virtual_mode=True)
        used by the deepagents virtual FS routing continues to work seamlessly with
        skills migrated to the plugins directory.
        """
        import os
        from k8s_autopilot.core.backend import get_project_root
        root = get_project_root()
        skills_dir = root / "skills"
        plugins_dir = root / "plugins"

        if not plugins_dir.is_dir():
            return

        # Ensure root / "skills" exists
        skills_dir.mkdir(parents=True, exist_ok=True)

        for domain_dir in plugins_dir.iterdir():
            if not domain_dir.is_dir():
                continue
            domain = domain_dir.name

            # Target directory for this domain under skills/
            domain_skills_dest = skills_dir / domain
            domain_skills_dest.mkdir(parents=True, exist_ok=True)

            # 1. Coordinator skill: plugins/{domain}/skills/coordinator -> skills/{domain}/coordinator
            coord_skill_src = domain_dir / "skills" / "coordinator"
            if not coord_skill_src.is_dir():
                coord_skill_src = domain_dir / "skills"
            if coord_skill_src.is_dir():
                coord_link = domain_skills_dest / "coordinator"
                if coord_link.exists() or coord_link.is_symlink():
                    try:
                        if coord_link.is_symlink() and Path(os.readlink(coord_link)).resolve() == coord_skill_src.resolve():
                            pass
                        else:
                            coord_link.unlink()
                            coord_link.symlink_to(coord_skill_src)
                    except Exception:
                        pass
                else:
                    try:
                        coord_link.symlink_to(coord_skill_src)
                    except Exception:
                        pass

            # 2. Agent skills: plugins/{domain}/agents/{agent}/skills/{agent} -> skills/{domain}/{agent}
            agents_dir = domain_dir / "agents"
            if agents_dir.is_dir():
                for agent_dir in agents_dir.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    agent_name = agent_dir.name
                    agent_skill_src = agent_dir / "skills" / agent_name
                    if not agent_skill_src.is_dir():
                        agent_skill_src = agent_dir / "skills"
                    if agent_skill_src.is_dir():
                        agent_link = domain_skills_dest / agent_name
                        if agent_link.exists() or agent_link.is_symlink():
                            try:
                                if agent_link.is_symlink() and Path(os.readlink(agent_link)).resolve() == agent_skill_src.resolve():
                                    pass
                                else:
                                    agent_link.unlink()
                                    agent_link.symlink_to(agent_skill_src)
                            except Exception:
                                pass
                        else:
                            try:
                                agent_link.symlink_to(agent_skill_src)
                            except Exception:
                                pass

    def get_project_skills_root(self) -> Path:
        """Get the base project skills directory."""
        if self._project_skills_root is None:
            from k8s_autopilot.core.backend import get_project_root
            self._project_skills_root = get_project_root() / "skills"
        return self._project_skills_root

    def discover_skills(self, force: bool = False) -> None:
        """Scan standard and project directories to discover SKILL.md files."""
        self._ensure_skills_symlinks()
        with self._lock:
            if self._is_discovered and not force:
                return

            self._skills.clear()
            roots: List[tuple[Path, str]] = []

            # 1. Project level (primary)
            project_skills = self.get_project_skills_root()
            if project_skills.exists() and project_skills.is_dir():
                roots.append((project_skills, "project"))

            # 2. Project custom/alias level
            from k8s_autopilot.core.backend import get_project_root
            root = get_project_root()
            for path_name in (".agents/skills", ".deepagents/skills"):
                custom_path = root / path_name
                if custom_path.exists() and custom_path.is_dir():
                    roots.append((custom_path, "project-custom"))

            # 3. User home level
            home = Path.home()
            for path_name in (".agents/skills", ".deepagents/agent/skills"):
                user_path = home / path_name
                if user_path.exists() and user_path.is_dir():
                    roots.append((user_path, "user"))

            # Scan and parse SKILL.md files under all roots
            for root_dir, source_type in roots:
                self._scan_root_directory(root_dir, source_type)

            # 4. Plugins level (new unified architecture)
            from k8s_autopilot.core.backend import get_project_root
            plugins_dir = get_project_root() / "plugins"
            if plugins_dir.exists() and plugins_dir.is_dir():
                self._scan_plugins_directory(plugins_dir, "project-plugin")

            self._is_discovered = True
            logger.info(
                "Skills discovery completed",
                extra={"total_skills": len(self._skills), "sources": [str(r[0]) for r in roots] + [str(plugins_dir)]},
            )

    def _scan_root_directory(self, root_dir: Path, source_type: str) -> None:
        """Recursively scan a root directory for folders containing SKILL.md."""
        for skill_file in root_dir.rglob("SKILL.md"):
            if not skill_file.is_file() or skill_file.name.startswith("."):
                continue

            skill_folder = skill_file.parent
            skill_name = skill_folder.name

            # Parse the file contents
            skill_data = self._parse_skill_file(skill_file, fallback_name=skill_name)
            if not skill_data:
                continue

            # Determine domain from directory path
            try:
                rel_path = skill_file.relative_to(root_dir)
                parts = rel_path.parts
                if len(parts) >= 3:
                    domain = parts[0]
                else:
                    domain = "global"
            except ValueError:
                domain = "global"

            # Compute virtual path (e.g., /skills/helm-operator/helm-generator)
            try:
                rel_folder = skill_folder.relative_to(root_dir)
                virtual_path = f"/skills/{rel_folder.as_posix()}"
            except ValueError:
                virtual_path = f"/skills/{skill_name}"

            metadata: SkillMetadata = {
                "name": skill_data["name"],
                "description": skill_data["description"],
                "domain": domain,
                "path": str(skill_file),
                "virtual_path": virtual_path,
                "frontmatter": skill_data["frontmatter"],
                "system_prompt": skill_data["system_prompt"],
            }

            self._skills[metadata["name"]] = metadata

    def _scan_plugins_directory(self, plugins_dir: Path, source_type: str) -> None:
        """Scan plugins directory for coordinator and subagent skills."""
        for domain_dir in sorted(plugins_dir.iterdir()):
            if not domain_dir.is_dir():
                continue
            domain = domain_dir.name

            # Coordinator skill: plugins/{domain}/skills/coordinator/SKILL.md or plugins/{domain}/skills/SKILL.md
            coord_skill = domain_dir / "skills" / "coordinator" / "SKILL.md"
            if not coord_skill.is_file():
                coord_skill = domain_dir / "skills" / "SKILL.md"
            if coord_skill.is_file():
                self._add_plugin_skill(coord_skill, domain, f"/skills/{domain}/coordinator", source_type)

            # Agent skills: plugins/{domain}/agents/{agent}/skills/{agent}/SKILL.md or plugins/{domain}/agents/{agent}/skills/SKILL.md
            agents_dir = domain_dir / "agents"
            if agents_dir.is_dir():
                for agent_dir in sorted(agents_dir.iterdir()):
                    if not agent_dir.is_dir():
                        continue
                    agent_name = agent_dir.name
                    agent_skill = agent_dir / "skills" / agent_name / "SKILL.md"
                    if not agent_skill.is_file():
                        agent_skill = agent_dir / "skills" / "SKILL.md"
                    if agent_skill.is_file():
                        self._add_plugin_skill(agent_skill, domain, f"/skills/{domain}/{agent_name}", source_type)

    def _add_plugin_skill(self, skill_file: Path, domain: str, virtual_path: str, source_type: str) -> None:
        skill_folder = skill_file.parent
        skill_name = skill_folder.parent.name if skill_folder.name == "skills" else skill_folder.name

        skill_data = self._parse_skill_file(skill_file, fallback_name=skill_name)
        if not skill_data:
            return

        metadata: SkillMetadata = {
            "name": skill_data["name"],
            "description": skill_data["description"],
            "domain": domain,
            "path": str(skill_file),
            "virtual_path": virtual_path,
            "frontmatter": skill_data["frontmatter"],
            "system_prompt": skill_data["system_prompt"],
        }

        existing = self._skills.get(metadata["name"])
        if existing:
            logger.debug(
                f"Overriding skill '{metadata['name']}' from {existing['path']} with {metadata['path']}"
            )
        self._skills[metadata["name"]] = metadata

    def _parse_skill_file(self, file_path: Path, fallback_name: str) -> Optional[Dict[str, Any]]:
        """Parse SKILL.md and extract YAML frontmatter + markdown body."""
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(f"Skipping skill {file_path}: unable to read file ({exc})")
            return None

        # Extract YAML frontmatter (delimited by ---)
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
        if not match:
            logger.warning(
                f"Skipping skill {file_path}: missing YAML frontmatter. Must start with '---' delimited block."
            )
            return None

        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            logger.warning(f"Skipping skill {file_path}: invalid YAML frontmatter ({exc})")
            return None

        if not isinstance(frontmatter, dict):
            logger.warning(f"Skipping skill {file_path}: frontmatter must be a key-value mapping.")
            return None

        name_val = frontmatter.get("name")
        description_val = frontmatter.get("description", "")

        name = str(name_val).strip() if name_val else fallback_name
        description = str(description_val).strip()

        return {
            "name": name,
            "description": description,
            "frontmatter": frontmatter,
            "system_prompt": match.group(2).strip(),
        }

    # ── Public APIs ────────────────────────────────────────────────────────

    def list_skills(self) -> List[SkillMetadata]:
        """List all discovered skills."""
        self.discover_skills()
        return list(self._skills.values())

    def get_skill(self, name: str) -> Optional[SkillMetadata]:
        """Retrieve a specific skill by name."""
        self.discover_skills()
        return self._skills.get(name)

    def get_skills_by_domain(self, domain: str) -> List[SkillMetadata]:
        """Get all skills belonging to a specific domain (e.g. 'helm-operator')."""
        self.discover_skills()
        return [skill for skill in self._skills.values() if skill["domain"] == domain]

    def get_skill_paths_for_domain(self, domain: str) -> List[str]:
        """Get virtual paths for all skills belonging to a domain."""
        skills = self.get_skills_by_domain(domain)
        return [skill["virtual_path"] for skill in skills]

    def get_skill_sources_for_domain(self, domain: str) -> List[str]:
        """Get domain-level parent directories for SkillsMiddleware source discovery.

        Unlike ``get_skill_paths_for_domain()`` which returns individual skill
        virtual paths (e.g., ``/skills/helm-operator/helm-generator``),
        this method returns the *parent directory* that SkillsMiddleware should
        ``ls()`` to discover subdirectories containing ``SKILL.md``.

        For domain ``helm-operator`` with skills at
        ``/skills/helm-operator/helm-generator``, ``/skills/helm-operator/helm-operation``, etc.,
        this returns ``["/skills/helm-operator/"]``.

        Returns:
            List of unique parent directory virtual paths.
        """
        skills = self.get_skills_by_domain(domain)
        if not skills:
            return []
        # Deduplicate parent directories
        parents: dict[str, None] = {}
        for skill in skills:
            vpath = skill["virtual_path"].rstrip("/")
            # Parent is one level up from the skill directory
            parent = "/".join(vpath.split("/")[:-1]) + "/"
            parents[parent] = None
        return sorted(parents.keys())

    def seed_skills_files(self, skill_paths: List[str]) -> Dict[str, Any]:
        """Load and return virtual file data for the given virtual skill paths.

        Used by seed_files() to populate the Virtual Filesystem with the SKILL.md
        and any references under the skill directory.
        """
        from deepagents.backends.utils import create_file_data

        files: Dict[str, Any] = {}
        self.discover_skills()

        project_skills = self.get_project_skills_root()

        for vpath in skill_paths:
            # Clean virtual path (e.g., '/skills/helm-operator/helm-generator')
            vpath_cleaned = vpath.rstrip("/")
            
            # Find all matching skills (supports exact matching and directory prefix matching)
            matched_skills = []
            for skill in self._skills.values():
                skill_vpath = skill["virtual_path"].rstrip("/")
                if (skill_vpath == vpath_cleaned) or (skill_vpath.startswith(vpath_cleaned + "/")):
                    matched_skills.append(skill)

            for matched_skill in matched_skills:
                skill_file = Path(matched_skill["path"])
                skill_dir = skill_file.parent
                
                # Recursively add all files in this skill directory
                for path in skill_dir.rglob("*"):
                    if path.is_file() and not path.name.startswith("."):
                        try:
                            # Map to relative path from the root of the source directory
                            # E.g., if path is under project skills, relative to project_skills
                            # Otherwise relative to the parent of the skill folder
                            rel_path = path.relative_to(skill_dir).as_posix()
                            child_vpath = f"{matched_skill['virtual_path']}/{rel_path}"
                            
                            files[child_vpath] = create_file_data(path.read_text(encoding="utf-8"))
                        except Exception as exc:
                            logger.warning(f"Error seeding skill file {path} under {vpath}: {exc}")

        return files


# ── Global Singleton and lookup helper ─────────────────────────────────────

_registry_instance: Optional[SkillRegistry] = None
_registry_lock = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    """Return the global SkillRegistry singleton."""
    global _registry_instance
    if _registry_instance is None:
        with _registry_lock:
            if _registry_instance is None:
                _registry_instance = SkillRegistry()
    return _registry_instance
