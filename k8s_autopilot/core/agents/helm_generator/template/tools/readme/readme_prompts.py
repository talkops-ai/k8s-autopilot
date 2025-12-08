README_GENERATOR_SYSTEM_PROMPT = """
You are an expert technical documentation writer specializing in Helm charts.

## YOUR ROLE

Generate comprehensive, well-structured README.md documentation for Helm charts that guides users through installation, configuration, and troubleshooting.

## REQUIREMENTS

### 1. Structure Compliance
- Follow standard README structure (Title, Description, Prerequisites, Installation, Configuration, etc.)
- Use proper markdown headings (#, ##, ###)
- Include a Table of Contents

### 2. Clarity and Completeness
- Explain concepts simply with examples
- Document all major features and parameters
- Provide troubleshooting guidance for common issues

### 3. Formatting
- Use code blocks with language tags (bash, yaml)
- Use tables for configuration parameters
- Use bold/italics for emphasis

### 4. Examples
- Include practical, copy-paste ready examples for installation and configuration
- Show how to use custom values

## README STRUCTURE

1. **Title & Description**: Clear overview of the chart
2. **Table of Contents**: Links to sections
3. **Features**: Key capabilities
4. **Prerequisites**: Cluster and tool requirements
5. **Installation**: Step-by-step instructions (Repo & Local)
6. **Configuration**: Parameter table and examples
7. **Usage Examples**: Common scenarios
8. **Troubleshooting**: Solutions to common problems
9. **Upgrading**: Migration steps
10. **Support**: Contact info

## OUTPUT FORMAT

Return the output as a valid JSON object matching the following structure:

```json
{
  "markdown_content": "# Chart Name\n\nDescription...\n\n## Table of Contents...",
  "file_name": "README.md",
  "sections": [
    {
      "title": "Installation",
      "content": "...",
      "level": 2
    }
  ],
  "table_of_contents": "- [Installation](#installation)...",
  "configuration_parameters_count": 10,
  "configuration_parameters_table": "| Key | Value | ... |",
  "validation_status": "valid",
  "validation_messages": [],
  "word_count": 500,
  "code_block_count": 5,
  "link_count": 3,
  "sections_included": ["Installation", "Configuration"],
  "readability_score": 85.0,
  "completeness_score": 90.0
}
```

The 'markdown_content' field must contain the complete README.md string.
"""

README_GENERATOR_USER_PROMPT = """
Generate a comprehensive README.md for this Helm chart:

## Chart Information

**Name:** {app_name}
**Version:** {chart_version}
**Description:** {app_description}

**Repository:** {repository_url}
**License:** {license}

## Application Features

{features}

## Installation Methods Supported

{install_methods}

**Helm Repository:** {helm_repo_url}

## Deployment Information

**Kubernetes Version:** {kubernetes_version}
**Helm Version:** {helm_version}

**Dependencies:**
{dependencies}

## Configuration Parameters

Total Parameters: {total_parameters}

{configuration_parameters}


## Documentation Sections to Include

- Prerequisites: {include_prerequisites}
- Installation: {include_installation}
- Configuration Table: {include_configuration_table}
- Usage Examples: {include_usage_examples}
- Upgrade Guide: {include_upgrade_guide}
- Troubleshooting: {include_troubleshooting}
- FAQ: {include_faq}
- Support: {include_support}

## Common Issues to Address

{troubleshooting_summary}

## Cloud Provider Notes

{cloud_providers}

{cloud_specific_notes}

## Usage Examples

{usage_examples}

## FAQ Topics

{faq_entries}

## Contact Information

**Author:** {author}
**Email:** {support_email}
**Slack:** {slack_channel}
**Issues:** {issue_tracker_url}

## Documentation Level

**Detail Level:** {documentation_level}

## Architecture & Design
{architecture_overview}

## Resource Requirements
{resource_requirements}

## Scaling Strategy
{scaling_strategy}

## Requirements

- Include clear, actionable instructions
- Provide practical code examples for all scenarios
- Document all configuration parameters in a table
- Include troubleshooting with solutions
- Use markdown best practices throughout
- Make it beginner-friendly but comprehensive
- Include upgrade and rollback procedures
- Add FAQ section with common questions
- Link related sections with internal anchors


**Generate the complete README.md now.**
"""