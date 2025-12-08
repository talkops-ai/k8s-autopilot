# k8s-autopilot

> **Production-Grade Multi-Agent Framework for Kubernetes Helm Chart Generation**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![LangChain](https://img.shields.io/badge/LangChain-v1.0-green.svg)](https://www.langchain.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-v1.0-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**k8s-autopilot** is an intelligent, multi-agent framework that automates the complete lifecycle of Kubernetes Helm chart generation. Built on LangChain and LangGraph, it transforms natural language requirements into production-ready Helm charts following Bitnami-quality standards.

---

## 🎯 What It Does

k8s-autopilot automates the end-to-end process of creating enterprise-grade Helm charts:

1. **📋 Planning**: Analyzes requirements, validates completeness, and designs Kubernetes architecture
2. **⚙️ Generation**: Generates Helm templates, values files, and documentation
3. **✅ Validation**: Validates charts, performs security scanning, and ensures production readiness
4. **🔄 Self-Healing**: Automatically fixes common errors (YAML indentation, deprecated APIs, missing fields)
5. **👤 Human-in-the-Loop**: Requests approvals at critical workflow points

### Current Capabilities

**✅ Fully Supported**:
- Helm chart planning and architecture design
- Helm chart template generation (Deployment, Service, Ingress, ConfigMap, Secret, HPA, PDB, NetworkPolicy, etc.)
- Traefik IngressRoute generation (modern CRD-based routing)
- Helm chart validation (lint, template rendering, cluster compatibility)
- Self-healing validation errors
- Human-in-the-loop approvals

**🚧 Planned** (Future Releases):
- Automated deployment to Kubernetes clusters
- CI/CD pipeline generation
- Application monitoring and observability setup
- Multi-cluster deployment strategies

---

## 🏗️ Architecture

k8s-autopilot follows a **hierarchical supervisor-with-swarms** pattern, leveraging LangChain's Deep Agents and LangGraph for orchestration.

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Supervisor Agent                              │
│  - Orchestrates workflow phases                                  │
│  - Manages HITL approvals                                        │
│  - Coordinates agent swarms                                      │
│  - Handles state transformation                                  │
└──────────────┬───────────────────────────────────────────────────┘
               │
    ┌──────────┴──────────┬──────────────┬──────────────────────┐
    │                     │              │                      │
    ▼                     ▼              ▼                      ▼
┌─────────────┐    ┌──────────────┐  ┌──────────────┐   ┌─────────────┐
│  Planner    │    │  Template    │  │  Generator   │   │  HITL       │
│  Agent      │───▶│  Coordinator │──▶│  Agent       │   │  Gates      │
│             │    │              │  │              │   │             │
│ (Deep       │    │ (LangGraph   │  │ (Deep        │   │ (Interrupt  │
│  Agent)     │    │  StateGraph) │  │  Agent)      │   │  Tools)     │
└─────────────┘    └──────────────┘  └──────────────┘   └─────────────┘
```

### Architecture Components

#### 1. **Supervisor Agent**
Central orchestrator that coordinates workflow phases and manages state flow between swarms.

**Key Features**:
- Tool-based delegation pattern (`create_agent()`)
- State transformation between supervisor and swarm schemas
- HITL gate management
- Stream processing with interrupt detection

📖 **[Supervisor Agent Documentation](./docs/supervisor/supervisor-agent-documentation.md)**

#### 2. **Planner Agent**
Deep agent that analyzes requirements and designs Kubernetes architecture.

**Key Features**:
- Requirement extraction from natural language
- Gap detection and HITL clarification requests
- Architecture planning following Bitnami standards
- Resource estimation and scaling strategy

📖 **[Planner Agent Documentation](./docs/planner/planner-agent-documentation.md)**

#### 3. **Template Coordinator Agent**
LangGraph-based coordinator that generates Helm chart templates and values files.

**Key Features**:
- 13 specialized generation tools
- Dependency-aware execution
- Phase-based workflow (core → conditional → documentation)
- Traefik IngressRoute generation

📖 **[Template Coordinator Documentation](./docs/template/template-coordinator-documentation.md)**

#### 4. **Generator Agent (Validator)**
Deep agent that validates, self-heals, and ensures charts are production-ready.

**Key Features**:
- Helm validation (lint, template, dry-run)
- Autonomous error fixing
- Retry logic with human escalation
- Workspace file management

📖 **[Generator Agent Documentation](./docs/generator/generator-agent-documentation.md)**

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Helm CLI installed and in PATH
- Kubernetes cluster (optional, for dry-run validation)
- LLM API key (OpenAI, Anthropic, etc.)

### Installation

```bash
# Clone the repository
git clone https://github.com/talkops-ai/k8s-autopilot.git
cd k8s-autopilot

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
export OPENAI_API_KEY="your-api-key"
# or
export ANTHROPIC_API_KEY="your-api-key"
```

### Basic Usage

```python
from k8s_autopilot.core.agents.supervisor_agent import create_k8sAutopilotSupervisorAgent
from k8s_autopilot.core.agents.helm_generator.planner.planning_swarm import create_planning_deep_agent
from k8s_autopilot.core.agents.helm_generator.template.template_coordinator import create_template_supervisor
from k8s_autopilot.core.agents.helm_generator.generator.generator_agent import create_validator_deep_agent

# Create agent swarms
planning_agent = create_planning_deep_agent()
template_agent = create_template_supervisor()
validator_agent = create_validator_deep_agent()

# Create supervisor
supervisor = create_k8sAutopilotSupervisorAgent(
    agents=[planning_agent, template_agent, validator_agent]
)

# Stream workflow execution
async for response in supervisor.stream(
    query_or_command="Create a Helm chart for nginx",
    context_id="session-123",
    task_id="task-456"
):
    if response.require_user_input:
        # Handle HITL interrupt
        user_response = input(response.content['question'])
        # Resume workflow
        async for response in supervisor.stream(
            Command(resume=user_response),
            context_id="session-123",
            task_id="task-456"
        ):
            print(response.content)
    else:
        print(response.content)
```

---

## 📚 Documentation

### Agent Documentation

- **[Supervisor Agent](./docs/supervisor/supervisor-agent-documentation.md)** - Central orchestrator
  - Architecture and tool-based delegation
  - State transformation and workflow orchestration
  - HITL gates and interrupt handling

- **[Planner Agent](./docs/planner/planner-agent-documentation.md)** - Requirements analysis and architecture planning
  - Requirement extraction and validation
  - Gap detection and HITL interactions
  - Architecture design and resource estimation

- **[Template Coordinator](./docs/template/template-coordinator-documentation.md)** - Helm chart generation
  - 13 generation tools and dependencies
  - Phase-based workflow execution
  - Traefik IngressRoute support

- **[Generator Agent](./docs/generator/generator-agent-documentation.md)** - Validation and self-healing
  - Helm validation tools
  - Autonomous error fixing
  - Retry logic and human escalation

### Specialized Documentation

- **[Traefik Comprehensive Guide](./docs/template/traefik-comprehensive-guide.md)** - Complete Traefik IngressRoute documentation
  - CRD definitions and matcher syntax
  - Middleware system and load balancing
  - TLS configuration and migration guide

### Tool Documentation

- **[Core Tools](./docs/template/tools-core-tools.md)** - Essential generation tools
- **[Conditional Tools](./docs/template/tools-conditional-tools.md)** - Conditional resource generation

---

## 🔄 Workflow

### Complete Workflow Sequence

```
User Request: "Create a Helm chart for nginx"
    ↓
1. Supervisor → Planner Agent
   - Extracts requirements
   - Detects gaps (if any) → HITL clarification
   - Designs architecture
   - Creates chart plan
    ↓
2. Supervisor → Template Coordinator
   - Generates Chart.yaml
   - Generates values.yaml
   - Generates templates (Deployment, Service, IngressRoute, etc.)
   - Generates README.md
    ↓
3. Supervisor → HITL Gate (Generation Review)
   - Shows generated artifacts
   - Requests workspace directory
   - Human approval required
    ↓
4. Supervisor → Generator Agent (Validator)
   - Writes chart files to workspace
   - Runs helm lint validation
   - Runs helm template validation
   - Runs helm dry-run validation
   - Self-heals errors (if possible)
   - Escalates to human (if needed)
    ↓
5. Supervisor → Final Notification
   - Workflow complete
   - Deployment instructions provided
```

### Human-in-the-Loop Gates

**Mandatory Gates**:
- **Generation Review**: After template generation completes
  - Review generated artifacts
  - Specify workspace directory
  - Approval required before validation

**Optional Gates**:
- **Planning Review**: Currently auto-proceeds (can be enabled)
- **Validation Escalation**: When validator cannot auto-fix errors

---

## 🛠️ Technology Stack

### Core Framework

- **LangChain v1.0**: LLM integration and tool framework
- **LangGraph v1.0**: Stateful graph orchestration
- **Deep Agents**: Multi-step reasoning and autonomous problem-solving
- **Pydantic v2**: Type-safe state schemas and validation

### Kubernetes & Helm

- **Helm CLI**: Chart validation and template rendering
- **Kubernetes API**: Cluster compatibility validation
- **Traefik CRDs**: Modern ingress routing

### State Management

- **PostgreSQL Checkpointer**: Persistent state storage (preferred)
- **MemorySaver**: In-memory checkpointing (fallback)
- **State Reducers**: Concurrent update handling

### LLM Providers

- **OpenAI**: GPT-4, GPT-3.5-turbo
- **Anthropic**: Claude Sonnet, Claude Opus
- **Configurable**: Via centralized `LLMProvider`

---

## 📋 Roadmap

### Current Release (v1.0.0)

**✅ Implemented**:
- [x] Multi-agent architecture with supervisor pattern
- [x] Planning agent with requirement extraction
- [x] Template coordinator with 13 generation tools
- [x] Generator agent with validation and self-healing
- [x] HITL gates for approval workflows
- [x] State transformation and persistence
- [x] Traefik IngressRoute support
- [x] Comprehensive documentation

### Future Releases

**v1.1.0 - Enhanced Validation** (Planned):
- [ ] Security scanning integration (Kubesec, Trivy)
- [ ] Policy compliance checking
- [ ] Helm unit test generation
- [ ] ArgoCD Application manifest generation

**v1.2.0 - Deployment Automation** (Planned):
- [ ] Automated deployment to Kubernetes clusters
- [ ] Deployment rollback capabilities
- [ ] Multi-cluster deployment strategies
- [ ] Deployment status monitoring

**v1.3.0 - CI/CD Integration** (Planned):
- [ ] GitHub Actions workflow generation
- [ ] GitLab CI pipeline generation
- [ ] Jenkins pipeline generation
- [ ] Automated testing integration

**v2.0.0 - Extended Capabilities** (Planned):
- [ ] Application monitoring setup (Prometheus, Grafana)
- [ ] Logging aggregation (ELK, Loki)
- [ ] Service mesh integration (Istio, Linkerd)
- [ ] Multi-cloud deployment support

---

## 🏛️ Architecture Principles

### 1. **Modular Swarm Design**
Each agent swarm is independently deployable and scalable, allowing for:
- Independent updates and versioning
- Horizontal scaling of individual swarms
- Easy addition of new capabilities

### 2. **Stateful Orchestration**
LangGraph provides:
- Explicit state management with reducers
- Checkpointing for resumable workflows
- Interrupt handling for HITL interactions

### 3. **Tool-Based Delegation**
Supervisor uses tool wrappers instead of manual routing:
- LLM decides routing dynamically
- Simplified graph building
- Easy to add new swarms

### 4. **Human-Centric Design**
HITL gates ensure:
- Human oversight at critical points
- Approval workflows for production readiness
- Escalation when autonomous fixes fail

### 5. **Self-Healing Capabilities**
Agents autonomously fix common errors:
- YAML indentation issues
- Deprecated API versions
- Missing required fields
- Retry logic with human escalation

---

## 🔐 Security & Best Practices

### Security Features

- **Input Validation**: All user inputs validated via Pydantic schemas
- **Path Sanitization**: File paths validated to prevent directory traversal
- **State Encryption**: Optional encryption for checkpoint data
- **RBAC Support**: Kubernetes RBAC validation in dry-run

### Best Practices

- **Bitnami Standards**: Charts follow Bitnami-quality conventions
- **CNCF Compliance**: Adheres to CNCF Helm chart best practices
- **Resource Limits**: All resources include requests and limits
- **Security Contexts**: Pod security standards enforced
- **Health Checks**: Liveness and readiness probes included

---

## 📊 State Management

### State Schemas

Each agent swarm uses its own state schema:

- **MainSupervisorState**: Supervisor workflow state
- **PlanningSwarmState**: Planning phase state
- **GenerationSwarmState**: Template generation state
- **ValidationSwarmState**: Validation phase state

### State Transformation

`StateTransformer` handles bidirectional conversion:
- Extracts relevant data from supervisor state
- Transforms to swarm-specific schemas
- Aggregates results back to supervisor state

### Persistence

- **PostgreSQL**: Preferred for production (persistent, scalable)
- **MemorySaver**: Fallback for development (ephemeral)
- **Checkpointing**: Automatic state persistence at each step

---

## 🧪 Testing

### Running Tests

```bash
# Run all tests
pytest

# Run specific test suite
pytest tests/test_planner.py
pytest tests/test_template_coordinator.py
pytest tests/test_generator.py

# Run with coverage
pytest --cov=k8s_autopilot tests/
```

### Test Coverage

- Unit tests for individual tools
- Integration tests for agent workflows
- End-to-end tests for complete workflows
- HITL interrupt handling tests

---

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

### Development Setup

```bash
# Clone repository
git clone https://github.com/talkops-ai/k8s-autopilot.git
cd k8s-autopilot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install development dependencies
pip install -r requirements-dev.txt

# Install pre-commit hooks
pre-commit install
```

### Code Style

- **Formatting**: Black
- **Linting**: Ruff
- **Type Checking**: mypy
- **Documentation**: Google-style docstrings

---

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **LangChain Team**: For the excellent LangChain and LangGraph frameworks
- **Bitnami**: For Helm chart best practices and standards
- **CNCF**: For Kubernetes and Helm community standards
- **Traefik**: For modern ingress routing capabilities

---

## 📞 Support

- **GitHub Issues**: [Report bugs or request features](https://github.com/talkops-ai/k8s-autopilot/issues)
- **Documentation**: See [docs/](./docs/) directory for detailed documentation
- **Discussions**: [GitHub Discussions](https://github.com/talkops-ai/k8s-autopilot/discussions)

---

## 🌟 Star Us

If you find k8s-autopilot helpful, please consider starring our repository:

⭐ [https://github.com/talkops-ai/k8s-autopilot](https://github.com/talkops-ai/k8s-autopilot)

---

## 📖 Additional Resources

- **[Architecture Documentation](./docs/k8s-autopilot-architecture.md)** - Detailed system architecture
- **[API Reference](./docs/api/)** - API documentation (coming soon)
- **[Examples](./examples/)** - Example workflows and use cases (coming soon)
- **[Changelog](./CHANGELOG.md)** - Version history and changes (coming soon)

---

**Built with ❤️ by the TalkOps team**
