"""
README.md generation tool.
"""
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Literal, Optional
import re
import yaml
from enum import Enum
from langchain.tools import tool, InjectedToolCallId, ToolRuntime
from langgraph.types import Command
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import ToolMessage, SystemMessage
from langchain_core.prompts import MessagesPlaceholder
from k8s_autopilot.core.state.base import GenerationSwarmState
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.config.config import Config
from k8s_autopilot.core.llm.llm_provider import LLMProvider
from .readme_prompts import (
    README_GENERATOR_SYSTEM_PROMPT,
    README_GENERATOR_USER_PROMPT,
)

readme_generator_logger = AgentLogger("ReadmeGenerator")

class DocumentationLevel(str, Enum):
    """Level of documentation detail"""
    BASIC = "basic"  # Essential information only
    STANDARD = "standard"  # Standard production documentation
    COMPREHENSIVE = "comprehensive"  # Detailed with examples

class InstallationMethod(str, Enum):
    """Methods for installing the Helm chart"""
    HELM_REPO = "helm_repo"  # From Helm repository
    HELM_GIT = "helm_git"  # From git repository
    HELM_LOCAL = "helm_local"  # From local directory
    HELM_TARBALL = "helm_tarball"  # From tarball

class CloudProvider(str, Enum):
    """Cloud provider specific instructions"""
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    KUBERNETES = "kubernetes"  # Generic Kubernetes

class ReadmeSection(BaseModel):
    """Hierarchical README section structure"""
    title: str = Field(..., description="Section title (h2 or h3)")
    content: str = Field(..., description="Section content in markdown")
    level: int = Field(2, ge=1, le=6, description="Heading level (1-6)")
    subsections: List['ReadmeSection'] = Field(default=[], description="Nested subsections")
    include_in_toc: bool = Field(True, description="Include in table of contents")
    
    @field_validator('title')
    @classmethod
    def validate_title_length(cls, v):
        if len(v) > 100:
            raise ValueError("Section title must be <= 100 characters")
        return v

# Forward reference for recursive type
ReadmeSection.update_forward_refs()

class PrerequisitesSection(BaseModel):
    """Prerequisites and dependencies"""
    kubernetes_version: str = Field("1.20+", description="Minimum Kubernetes version")
    helm_version: str = Field("3.0+", description="Minimum Helm version")
    storage_class: Optional[str] = Field(None, description="Required storage class")
    ingress_controller: Optional[str] = Field(None, description="Required ingress controller")
    cert_manager: bool = Field(False, description="cert-manager required")
    external_dns: bool = Field(False, description="external-dns required")
    monitoring_stack: bool = Field(False, description="Prometheus/monitoring stack required")
    additional_requirements: List[str] = Field(default=[], description="Custom requirements")

class ConfigurationParameter(BaseModel):
    """Single configuration parameter"""
    key: str = Field(..., description="Parameter key (e.g., 'image.repository')")
    default_value: str = Field(..., description="Default value")
    description: str = Field(..., description="Parameter description")
    data_type: str = Field(..., description="Data type (string, int, bool, list, object)")
    required: bool = Field(False, description="Is this parameter required?")
    example: Optional[str] = Field(None, description="Example value")
    allowed_values: List[str] = Field(default=[], description="Allowed values (for enums)")

class TroubleshootingEntry(BaseModel):
    """Troubleshooting entry"""
    problem: str = Field(..., description="Problem description")
    symptoms: List[str] = Field(..., min_items=1, description="Symptoms/error messages")
    solution: str = Field(..., description="Solution/fix")
    prevention: Optional[str] = Field(None, description="How to prevent this issue")
    references: List[str] = Field(default=[], description="Links to related documentation")

class UpgradeStrategy(BaseModel):
    """Upgrade strategy information"""
    breaking_changes: List[str] = Field(default=[], description="Breaking changes in this version")
    migration_steps: List[str] = Field(default=[], description="Migration steps required")
    rollback_procedure: str = Field(..., description="How to rollback if something goes wrong")
    testing_recommendations: List[str] = Field(default=[], description="Testing before upgrading")

# ============================================================
# MAIN INPUT SCHEMA
# ============================================================

class ReadmeGenerationInput(BaseModel):
    """
    Input schema for README.md generation.
    
    This tool is called AFTER all templates are generated,
    so it has access to complete chart information.
    
    Populated from:
    - planner_output: All planner data for context
    - generated_templates: All generated YAML files
    - values_yaml_content: Complete values.yaml content
    """
    
    # ============================================================
    # REQUIRED FIELDS
    # ============================================================
    
    app_name: str = Field(
        ...,
        description="Application name",
        pattern=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
    )
    
    chart_version: str = Field(
        "1.0.0",
        description="Helm chart version (semver)"
    )
    
    app_description: str = Field(
        ...,
        description="Short description of the application",
        max_length=500
    )
    
    planner_output: Dict[str, Any] = Field(
        ...,
        description="Complete planner output for context"
    )
    
    generated_templates: Dict[str, str] = Field(
        ...,
        description="All generated YAML templates (filename: content)"
    )
    
    values_yaml_content: str = Field(
        ...,
        description="Complete values.yaml content"
    )
    
    # ============================================================
    # DOCUMENTATION CONFIGURATION
    # ============================================================
    
    documentation_level: DocumentationLevel = Field(
        DocumentationLevel.STANDARD,
        description="Level of documentation detail"
    )
    
    include_prerequisites: bool = Field(True, description="Include prerequisites section")
    include_installation: bool = Field(True, description="Include installation instructions")
    include_configuration_table: bool = Field(True, description="Include configuration parameters table")
    include_usage_examples: bool = Field(True, description="Include usage examples")
    include_upgrade_guide: bool = Field(True, description="Include upgrade/rollback guide")
    include_troubleshooting: bool = Field(True, description="Include troubleshooting section")
    include_faq: bool = Field(True, description="Include FAQ section")
    include_support: bool = Field(True, description="Include support/contribution section")
    
    # ============================================================
    # PREREQUISITES CONFIGURATION
    # ============================================================
    
    prerequisites: PrerequisitesSection = Field(
        default_factory=PrerequisitesSection,
        description="Prerequisites and dependencies"
    )
    
    # ============================================================
    # CONFIGURATION PARAMETERS
    # ============================================================
    
    configuration_parameters: List[ConfigurationParameter] = Field(
        default=[],
        description="Configuration parameters extracted from values.yaml"
    )
    
    # ============================================================
    # TROUBLESHOOTING & SUPPORT
    # ============================================================
    
    troubleshooting_entries: List[TroubleshootingEntry] = Field(
        default=[],
        description="Common troubleshooting scenarios"
    )
    
    # ============================================================
    # UPGRADE INFORMATION
    # ============================================================
    
    upgrade_strategy: Optional[UpgradeStrategy] = Field(
        None,
        description="Information about upgrading from previous versions"
    )
    
    # ============================================================
    # INSTALLATION METHODS
    # ============================================================
    
    installation_methods: List[InstallationMethod] = Field(
        default=[InstallationMethod.HELM_REPO],
        description="Supported installation methods"
    )
    
    helm_repo_url: Optional[str] = Field(
        None,
        description="Helm repository URL (if using helm_repo method)"
    )
    
    helm_repo_name: Optional[str] = Field(
        None,
        description="Helm repository name (if using helm_repo method)"
    )
    
    # ============================================================
    # CLOUD-SPECIFIC INFORMATION
    # ============================================================
    
    cloud_providers: List[CloudProvider] = Field(
        default=[CloudProvider.KUBERNETES],
        description="Supported cloud providers with specific instructions"
    )
    
    cloud_specific_notes: Dict[str, str] = Field(
        default={},
        description="Cloud provider specific notes and warnings"
    )
    
    # ============================================================
    # METADATA AND LINKS
    # ============================================================
    
    author: Optional[str] = Field(None, description="Chart author/maintainer")
    license: Optional[str] = Field("MIT", description="License type")
    repository_url: Optional[str] = Field(None, description="GitHub/GitLab repository URL")
    documentation_url: Optional[str] = Field(None, description="External documentation URL")
    issue_tracker_url: Optional[str] = Field(None, description="Issue tracker URL")
    support_email: Optional[str] = Field(None, description="Support email address")
    slack_channel: Optional[str] = Field(None, description="Support Slack channel")
    
    # ============================================================
    # FAQ CONTENT
    # ============================================================
    
    faq_entries: List[Dict[str, str]] = Field(
        default=[],
        description="FAQ entries (question, answer pairs)"
    )
    
    # ============================================================
    # USAGE EXAMPLES
    # ============================================================
    
    usage_examples: List[Dict[str, str]] = Field(
        default=[],
        description="Usage examples (title, code pairs)"
    )
    
    # ============================================================
    # METADATA
    # ============================================================
    
    namespace: str = Field("default", description="Default namespace for examples")
    
    tags: List[str] = Field(default=[], description="Chart tags/keywords")
    
    # ============================================================
    # VALIDATORS
    # ============================================================
    
    @field_validator('chart_version')
    @classmethod
    def validate_semver(cls, v):
        """Validate semantic versioning"""
        if not re.match(r'^\d+\.\d+\.\d+(-[a-z0-9]+)?$', v):
            raise ValueError(f"Invalid semver: {v}")
        return v
    
    @field_validator('configuration_parameters')
    @classmethod
    def validate_parameters_unique(cls, v):
        """Ensure unique parameter keys"""
        keys = [p.key for p in v]
        duplicates = [k for k in keys if keys.count(k) > 1]
        if duplicates:
            raise ValueError(f"Duplicate parameter keys: {set(duplicates)}")
        return v
    
    @field_validator('helm_repo_url')
    @classmethod
    def validate_helm_repo_if_needed(cls, v, values):
        """Validate helm_repo_url is set if helm_repo installation method is used"""
        if InstallationMethod.HELM_REPO in values.get('installation_methods', []):
            if not v:
                raise ValueError("helm_repo_url required when using HELM_REPO installation method")
        return v
    
    class Config:
        use_enum_values = False
        extra = "forbid"


class ReadmeGenerationOutput(BaseModel):
    """Output schema for README.md generation - simplified to only require markdown_content"""
    
    # Only mandatory field - the actual README content
    markdown_content: str = Field(
        ...,
        description="Complete README.md content in markdown"
    )
    
    # Optional fields - all have defaults or are optional
    file_name: str = Field(default="README.md", description="Output filename")
    
    sections: List[ReadmeSection] = Field(default=[], description="Hierarchical section structure")
    
    table_of_contents: str = Field(default="", description="Generated table of contents (markdown links)")
    
    configuration_parameters_count: int = Field(
        default=0,
        ge=0,
        description="Number of configuration parameters documented (0 if chart has no configurable parameters)"
    )
    
    configuration_parameters_table: str = Field(
        default="",
        description="Markdown table of configuration parameters"
    )
    
    validation_status: Literal["valid", "warning", "error"] = Field(default="valid")
    validation_messages: List[str] = Field(default=[])
    
    metadata: Dict[str, Any] = Field(default={})
    
    # README-specific metadata (all optional)
    word_count: int = Field(default=0, description="Total word count")
    code_block_count: int = Field(default=0, description="Number of code examples")
    link_count: int = Field(default=0, description="Number of links/references")
    sections_included: List[str] = Field(
        default=[],
        description="List of sections included (e.g., 'Installation', 'Troubleshooting')"
    )
    
    # Quality metrics (optional)
    readability_score: float = Field(
        default=0.0,
        ge=0,
        le=100,
        description="Estimated readability score (0-100)"
    )
    
    completeness_score: float = Field(
        default=0.0,
        ge=0,
        le=100,
        description="Documentation completeness (0-100)"
    )
    
    class Config:
        extra = "forbid"

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def extract_configuration_parameters(
    values_yaml_content: str,
    planner_output: Dict[str, Any]
) -> List[ConfigurationParameter]:
    """Extract configuration parameters from values.yaml"""
    
    try:
        values = yaml.safe_load(values_yaml_content)
    except:
        return []
    
    parameters = []
    
    def flatten_dict(d, prefix='', parent_desc=''):
        """Recursively flatten YAML structure"""
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            
            if isinstance(value, dict):
                # Recursively process nested dicts
                flatten_dict(value, full_key, f"{parent_desc} - {key}")
            else:
                # Convert value to string
                str_value = str(value) if value is not None else "null"
                
                # Determine data type
                if isinstance(value, bool):
                    data_type = "bool"
                elif isinstance(value, int):
                    data_type = "int"
                elif isinstance(value, list):
                    data_type = "list"
                elif isinstance(value, dict):
                    data_type = "object"
                else:
                    data_type = "string"
                
                # Extract description from comments (if available)
                description = f"Configuration for {full_key.replace('.', ' ')}"
                
                parameters.append(ConfigurationParameter(
                    key=full_key,
                    default_value=str_value,
                    description=description,
                    data_type=data_type,
                    required=False
                ))
    
    if values:
        flatten_dict(values)
    
    return sorted(parameters, key=lambda p: p.key)


def generate_configuration_table(params: List[ConfigurationParameter]) -> str:
    """Generate markdown table of configuration parameters"""
    
    if not params:
        return "No configuration parameters."
    
    lines = [
        "| Parameter | Default | Description | Type |",
        "|-----------|---------|-------------|------|"
    ]
    
    for param in params[:50]:  # Limit to 50 most important params
        # Escape markdown special chars
        desc = param.description.replace('|', '\\|')[:60]
        lines.append(
            f"| `{param.key}` | `{param.default_value}` | {desc} | {param.data_type} |"
        )
    
    if len(params) > 50:
        lines.append(f"| ... | ... | *+{len(params) - 50} more parameters* | ... |")
    
    return "\n".join(lines)


def parse_markdown_sections(content: str) -> List[ReadmeSection]:
    """Parse markdown content into hierarchical sections"""
    
    sections = []
    lines = content.split('\n')
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Match heading pattern
        match = re.match(r'^(#+)\s+(.+)$', line)
        if match:
            level = len(match.group(1))
            title = match.group(2)
            
            # Collect content until next heading
            content_lines = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if re.match(r'^#+\s+', next_line):
                    break
                content_lines.append(next_line)
                i += 1
            
            section_content = '\n'.join(content_lines).strip()
            
            sections.append(ReadmeSection(
                title=title,
                content=section_content,
                level=level,
                include_in_toc=(level <= 2)
            ))
        else:
            i += 1
    
    return sections


def generate_table_of_contents(sections: List[ReadmeSection]) -> str:
    """Generate markdown table of contents"""
    
    lines = ["## Table of Contents\n"]
    
    for section in sections:
        if section.include_in_toc and section.level <= 3:
            # Create anchor from title
            anchor = section.title.lower().replace(' ', '-').replace('.', '')
            indent = "  " * (section.level - 1)
            lines.append(f"{indent}- [{section.title}](#{anchor})")
    
    return "\n".join(lines)


def extract_included_sections(markdown_content: str) -> List[str]:
    """Extract list of sections included in README"""
    
    sections = []
    headings = re.findall(r'^##\s+(.+)$', markdown_content, re.MULTILINE)
    
    for heading in headings:
        sections.append(heading)
    
    return sections


def calculate_readability_score(content: str) -> float:
    """Calculate readability score based on various metrics"""
    
    score = 50.0  # Base score
    
    # Check for structure
    if re.search(r'^# ', content, re.MULTILINE):
        score += 10
    if len(re.findall(r'^## ', content, re.MULTILINE)) >= 5:
        score += 10
    
    # Check for examples
    code_blocks = len(re.findall(r'```', content)) // 2
    if code_blocks >= 3:
        score += 10
    
    # Check for formatting
    if re.search(r'\*\*.*?\*\*', content):  # Bold
        score += 5
    if re.search(r'`.*?`', content):  # Code snippets
        score += 5
    
    # Check for links
    links = len(re.findall(r'\[.*?\]\(.*?\)', content))
    if links >= 5:
        score += 10
    
    # Check for lists
    if re.search(r'^\s*[-*+]\s', content, re.MULTILINE):
        score += 10
    
    return min(score, 100.0)


def calculate_completeness_score(
    input_data: ReadmeGenerationInput,
    sections_included: List[str]
) -> float:
    """Calculate documentation completeness score"""
    
    score = 0.0
    total_possible = 0.0
    
    sections_to_check = {
        'prerequisites': input_data.include_prerequisites,
        'installation': input_data.include_installation,
        'configuration': input_data.include_configuration_table,
        'troubleshooting': input_data.include_troubleshooting,
        'upgrade': input_data.include_upgrade_guide,
        'faq': input_data.include_faq,
    }
    
    for section_name, should_include in sections_to_check.items():
        if should_include:
            total_possible += 1
            if any(section_name.lower() in sec.lower() for sec in sections_included):
                score += 1
    
    if total_possible == 0:
        return 100.0
    
    return (score / total_possible) * 100

def extract_app_features(planner_output: Dict[str, Any]) -> List[str]:
    """Extract application features from planner output"""
    
    planner_output = planner_output or {}
    app_analysis = planner_output.get("application_analysis", {}) or {}
    parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
    features = []
    
    # Scalability
    scalability = app_analysis.get("scalability", {}) or {}
    if scalability.get("hpa_enabled"):
        features.append("Horizontal Pod Autoscaling (HPA) enabled")
    
    # Security
    security = parsed_reqs.get("security", {}) or {}
    if security.get("network_policies"):
        features.append("Network Security Policies")
    if security.get("tls_encryption"):
        features.append("TLS Encryption")
        
    # High Availability
    deployment = parsed_reqs.get("deployment", {}) or {}
    if deployment.get("high_availability"):
        features.append("High Availability Configuration")
    
    # Storage
    storage = app_analysis.get("storage", {}) or {}
    if storage.get("persistent_storage"):
        features.append("Persistent Storage Support")
    
    # Monitoring (check dependencies or analysis)
    deps = planner_output.get("dependencies", {}) or {}
    sidecars = deps.get("sidecars_needed", []) or []
    if any("metrics" in (s or {}).get("name", "").lower() or "prometheus" in (s or {}).get("image", "").lower() for s in sidecars):
        features.append("Built-in Prometheus Metrics Exporter")
        
    # Logging
    if any("logging" in (s or {}).get("name", "").lower() or "fluent" in (s or {}).get("image", "").lower() for s in sidecars):
        features.append("Centralized Logging Integration")
        
    # Database
    if deps.get("helm_dependencies"):
        features.append("Integrated Database/Service Dependencies")

    return features or ["Production-ready deployment", "Customizable configuration", "Kubernetes native"]

def format_features(features: List[str]) -> str:
    """Format features as bullet list"""
    return "\n".join([f"- {f}" for f in features])


def bool_to_yes_no(value: bool) -> str:
    """Convert boolean to Yes/No"""
    return "Yes" if value else "No"

def extract_architecture_overview(planner_output: Dict[str, Any]) -> str:
    """Extract architecture design decisions and rationale"""
    planner_output = planner_output or {}
    architecture = planner_output.get("kubernetes_architecture", {}) or {}
    decisions = architecture.get("design_decisions", []) or []
    
    if not decisions:
        return "Standard Kubernetes deployment architecture."
        
    lines = []
    for d in decisions:
        d_dict = d or {}
        lines.append(f"### {d_dict.get('category', 'General').replace('_', ' ').title()}")
        lines.append(f"- **Decision:** {d_dict.get('decision', '')}")
        lines.append(f"- **Rationale:** {d_dict.get('rationale', '')}")
        if d_dict.get('alternatives'):
            lines.append(f"- *Alternatives Considered:* {d_dict.get('alternatives')}")
        lines.append("")
        
    return "\n".join(lines)

def extract_resource_requirements(planner_output: Dict[str, Any]) -> str:
    """Extract resource estimation for different environments"""
    planner_output = planner_output or {}
    resources = planner_output.get("resource_estimation", {}) or {}
    
    if not resources:
        return "No specific resource requirements defined."
        
    lines = ["| Environment | CPU Request | CPU Limit | Memory Request | Memory Limit |",
             "|-------------|-------------|-----------|----------------|--------------|"]
             
    for env in ['dev', 'staging', 'prod']:
        if env in resources:
            env_resources = resources[env] or {}
            reqs = env_resources.get("requests", {}) or {}
            limits = env_resources.get("limits", {}) or {}
            lines.append(f"| {env.upper()} | {reqs.get('cpu', '-')} | {limits.get('cpu', '-')} | {reqs.get('memory', '-')} | {limits.get('memory', '-')} |")
            
    if len(lines) == 2: # Only header
        return "No specific resource requirements defined."
        
    return "\n".join(lines)

def extract_scaling_strategy(planner_output: Dict[str, Any]) -> str:
    """Extract scaling strategy for different environments"""
    planner_output = planner_output or {}
    scaling = planner_output.get("scaling_strategy", {}) or {}
    
    if not scaling:
        return "Standard horizontal pod autoscaling."
        
    lines = ["| Environment | Min Replicas | Max Replicas | Target CPU | Target Memory |",
             "|-------------|--------------|--------------|------------|---------------|"]
             
    for env in ['dev', 'staging', 'prod']:
        if env in scaling:
            s = scaling[env] or {}
            lines.append(f"| {env.upper()} | {s.get('min_replicas', '-')} | {s.get('max_replicas', '-')} | {s.get('target_cpu_utilization', '-')}% | {s.get('target_memory_utilization', '-')}% |")
            
    if len(lines) == 2:
        return "Standard horizontal pod autoscaling."
        
    return "\n".join(lines)

def extract_detailed_dependencies(planner_output: Dict[str, Any]) -> str:
    """Extract detailed dependencies including Helm charts, init containers, sidecars"""
    planner_output = planner_output or {}
    deps = planner_output.get("dependencies", {}) or {}
    lines = []
    
    # Helm Dependencies
    helm_deps = deps.get("helm_dependencies", []) or []
    if helm_deps:
        lines.append("### Helm Charts")
        for d in helm_deps:
            d_dict = d or {}
            lines.append(f"- **{d_dict.get('name')}** ({d_dict.get('version', 'latest')}): {d_dict.get('reason', 'Required dependency')}")
            
    # Init Containers
    init_containers = deps.get("init_containers_needed", []) or []
    if init_containers:
        lines.append("\n### Init Containers")
        for ic in init_containers:
            ic_dict = ic or {}
            lines.append(f"- **{ic_dict.get('name')}**: {ic_dict.get('purpose', 'Initialization')}")
            
    # Sidecars
    sidecars = deps.get("sidecars_needed", []) or []
    if sidecars:
        lines.append("\n### Sidecar Containers")
        for sc in sidecars:
            sc_dict = sc or {}
            lines.append(f"- **{sc_dict.get('name')}**: {sc_dict.get('purpose', 'Auxiliary function')}")
            
    return "\n".join(lines) if lines else "No external dependencies."

@tool
async def generate_readme(
    runtime: ToolRuntime[None, GenerationSwarmState],
    tool_call_id: InjectedToolCallId,
) -> Command:
    """Generate a README.md file for a Helm chart"""
    try:
        # Defensive check: ensure runtime.state exists
        if runtime.state is None:
            raise ValueError("runtime.state is None - cannot generate README.md")
        
        # Get input data from runtime state
        planner_output = runtime.state.get("planner_output", {}) or {}
        parsed_reqs = planner_output.get("parsed_requirements", {}) or {}
        generated_templates = runtime.state.get("generated_templates", {}) or {}
        template_variables = runtime.state.get("template_variables", []) or []
        values_yaml_content = runtime.state.get("values_yaml_content", "") or ""
        app_name = parsed_reqs.get("app_name", "myapp")
        chart_version = parsed_reqs.get("chart_version", "1.0.0")
        app_description = parsed_reqs.get("app_description", "")
        repository_url = parsed_reqs.get("repository_url", "")
        license = parsed_reqs.get("license", "MIT")
        helm_repo_url = parsed_reqs.get("helm_repo_url", "")
        kubernetes_version = parsed_reqs.get("kubernetes_version", "1.20+")
        helm_version = parsed_reqs.get("helm_version", "3.0+")


        # Extract configuration parameters from values.yaml
        config_params = extract_configuration_parameters(
            values_yaml_content,
            planner_output
        )
        
        # Extract detailed info from planner output
        architecture_overview = extract_architecture_overview(planner_output)
        resource_requirements = extract_resource_requirements(planner_output)
        scaling_strategy = extract_scaling_strategy(planner_output)
        dependencies_desc = extract_detailed_dependencies(planner_output)
        total_parameters = len(config_params)
        prerequisite = bool_to_yes_no(runtime.state.get("include_prerequisites", False))
        installation = bool_to_yes_no(runtime.state.get("include_installation", False))
        configuration_table = bool_to_yes_no(runtime.state.get("include_configuration_table", False))
        usage_examples = bool_to_yes_no(runtime.state.get("include_usage_examples", False))
        upgrade_guide = bool_to_yes_no(runtime.state.get("include_upgrade_guide", False))
        troubleshooting = bool_to_yes_no(runtime.state.get("include_troubleshooting", False))
        faq = bool_to_yes_no(runtime.state.get("include_faq", False))
        support = bool_to_yes_no(runtime.state.get("include_support", False))

        documentation_level = runtime.state.get("documentation_level", "standard")
        if hasattr(documentation_level, "value"):
            documentation_level = documentation_level.value

        # Handle Enums that might be strings
        providers = runtime.state.get("cloud_providers", [])
        cloud_providers_list = []
        for cp in providers:
            if hasattr(cp, "value"):
                cloud_providers_list.append(cp.value.upper())
            else:
                cloud_providers_list.append(str(cp).upper())
        cloud_providers = ", ".join(cloud_providers_list)
        
        cloud_notes = runtime.state.get("cloud_specific_notes", {})
        cloud_specific_notes_list = []
        for cp in providers:
            val = cp.value if hasattr(cp, "value") else str(cp)
            if val in cloud_notes:
                cloud_specific_notes_list.append(f"**{val.upper()}:** {cloud_notes[val]}")
        cloud_specific_notes = "\n".join(cloud_specific_notes_list) or "Standard configuration"

        usage_examples_desc = "\n".join([f"- {ex.get('title', 'Example')}: Include code example" for ex in runtime.state.get("usage_examples", [])[:3]]) if runtime.state.get("usage_examples", []) else "- Basic installation"

        faq_entries = "\n".join([f"- Q: {entry.get('question', 'Q')}" for entry in runtime.state.get("faq_entries", [])[:5]]) if runtime.state.get("faq_entries", []) else "Standard Kubernetes FAQs"

        author = runtime.state.get("author", "")
        support_email = runtime.state.get("support_email", "")
        slack_channel = runtime.state.get("slack_channel", "")
        issue_tracker_url = runtime.state.get("issue_tracker_url", "")
        
        # Build features from planner
        features = extract_app_features(planner_output)
        features_desc = format_features(features)
        # Build installation instructions based on methods
        methods = runtime.state.get("installation_methods", [])
        install_methods_list = []
        for m in methods:
            val = m.value if hasattr(m, "value") else str(m)
            install_methods_list.append(val.replace("_", " ").title())
        install_methods_desc = ", ".join(install_methods_list)
        
        # Generate configuration table
        configuration_parameters_table = generate_configuration_table(config_params)

        # Build troubleshooting summary
        troubleshooting_summary = "\n".join([f"- {entry.problem}" for entry in runtime.state.get("troubleshooting_entries", [])[:5]]) if runtime.state.get("troubleshooting_entries", []) else "Standard Kubernetes troubleshooting"

        readme_generator_logger.log_structured(
            level="INFO",
            message="Generating README.md",
            extra={
                "tool_call_id": tool_call_id,
            }
        )
        def escape_json_for_template(json_str):
            """Escape curly braces in JSON strings for template compatibility"""
            return json_str.replace('{', '{{').replace('}', '}}')

        formatted_user_query = README_GENERATOR_USER_PROMPT.format(
            app_name=app_name,
            chart_version=chart_version,
            app_description=app_description,
            repository_url=repository_url,
            license=license,
            features=features_desc,
            install_methods=install_methods_desc,
            helm_repo_url=helm_repo_url,
            kubernetes_version=kubernetes_version,
            helm_version=helm_version,
            dependencies=dependencies_desc,
            total_parameters=total_parameters,
            configuration_parameters=configuration_parameters_table,
            include_prerequisites=prerequisite,
            include_installation=installation,
            include_configuration_table=configuration_table,
            include_usage_examples=usage_examples,
            include_upgrade_guide=upgrade_guide,
            include_troubleshooting=troubleshooting,
            include_faq=faq,
            include_support=support,
            cloud_providers=cloud_providers,
            cloud_specific_notes=cloud_specific_notes,
            usage_examples=usage_examples_desc,
            faq_entries=faq_entries,
            author=author,
            support_email=support_email,
            slack_channel=slack_channel,
            issue_tracker_url=issue_tracker_url,
            documentation_level=documentation_level,
            troubleshooting_summary=troubleshooting_summary,
            architecture_overview=architecture_overview,
            resource_requirements=resource_requirements,
            scaling_strategy=scaling_strategy,
        )
        parser = PydanticOutputParser(pydantic_object=ReadmeGenerationOutput)

        # Escape user query for Helm syntax
        escaped_user_query = formatted_user_query.replace("{", "{{").replace("}", "}}")
        
        # Use MessagesPlaceholder to bypass template parsing for system prompt
        prompt = ChatPromptTemplate.from_messages([
            MessagesPlaceholder("system_message"),
            ("user", escaped_user_query),
            ("user", "Please respond with valid JSON matching the ReadmeGenerationOutput schema.")
        ]).partial(
            format_instructions=parser.get_format_instructions(),
        )
        config = Config()
        llm_config = config.get_llm_config()
        higher_llm_config = config.get_llm_higher_config()
        model = LLMProvider.create_llm(
            provider=llm_config['provider'],
            model=llm_config['model'],
            temperature=llm_config['temperature'],
            max_tokens=llm_config['max_tokens']
        )

        higher_model = LLMProvider.create_llm(
            provider=higher_llm_config['provider'],
            model=higher_llm_config['model'],
            temperature=higher_llm_config['temperature'],
            max_tokens=higher_llm_config['max_tokens']
        )
        chain = prompt | higher_model | parser
        # Pass system message directly
        response = await chain.ainvoke({
            "system_message": [SystemMessage(content=README_GENERATOR_SYSTEM_PROMPT)]
        })
        readme_generator_logger.log_structured(
            level="INFO",
            message="README.md generated",
            extra={
                "response": response.model_dump(),
                "tool_call_id": tool_call_id
            }
        )
        response_json = response.model_dump()
        readme_content = response_json.get("markdown_content", "")
        file_name = response_json.get("file_name", "")
        tool_message = ToolMessage(
                content="README.md generated successfully.",
                tool_call_id=tool_call_id
        )
        # Update state tracking
        current_completed_tools = runtime.state.get("completed_tools", [])
        if "generate_readme" not in current_completed_tools:
            completed_tools = current_completed_tools + ["generate_readme"]
        else:
            completed_tools = current_completed_tools

        # Update tool results
        current_tool_results = runtime.state.get("tool_results", {})
        tool_results = {
            **current_tool_results,
            "generate_readme": {
                "status": "success",
                "output": response_json,
                "validation_messages": response_json.get("validation_messages", [])
            }
        }

        # Update generation metadata
        current_metadata = runtime.state.get("generation_metadata", {})
        generation_metadata = current_metadata.copy()
        generation_metadata["tools_executed"] = completed_tools
        if "quality_scores" not in generation_metadata:
            generation_metadata["quality_scores"] = {}
        score = response_json.get("completeness_score", 0) / 100.0
        generation_metadata["quality_scores"]["generate_readme"] = score

        # Reset retry count
        current_coordinator_state = runtime.state.get("coordinator_state", {})
        coordinator_state = current_coordinator_state.copy()
        coordinator_state["current_retry_count"] = 0

        return Command(
            update={
                "generated_templates": {
                    file_name: readme_content
                },
                "messages": [tool_message],
                # State tracking updates
                "completed_tools": completed_tools,
                "tool_results": tool_results,
                "generation_metadata": generation_metadata,
                "coordinator_state": coordinator_state,
                "next_action": "coordinator"
            },
        )
    except Exception as e:
        # Log detailed error information for debugging
        error_context = {
            "error": str(e),
            "error_type": type(e).__name__,
            "tool_call_id": tool_call_id
        }
        
        # Try to log state information if available (without causing another error)
        try:
            if runtime and runtime.state:
                error_context["state_keys"] = list(runtime.state.keys()) if hasattr(runtime.state, 'keys') else "N/A"
                error_context["has_planner_output"] = "planner_output" in runtime.state if runtime.state else False
        except:
            pass  # Don't fail on logging
        
        readme_generator_logger.log_structured(
            level="ERROR",
            message=f"Error generating README.md: {e}",
            extra=error_context
        )
        # Re-raise exception for coordinator error handling
        raise e