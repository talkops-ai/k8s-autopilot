# k8s-autopilot

> **Production-Grade Multi-Agent Framework for Kubernetes Helm Chart Generation**

[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/hFt5DAYEVx) [![Docker Hub](https://img.shields.io/badge/Docker%20Hub-sandeep2014/k8s--autopilot-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://hub.docker.com/r/sandeep2014/k8s-autopilot) [![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg?style=for-the-badge)](LICENSE) [![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/) [![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-FF6B6B?style=for-the-badge)](https://github.com/langchain-ai/langgraph) [![A2A Protocol](https://img.shields.io/badge/Google%20A2A-Protocol-4285F4?style=for-the-badge)](https://github.com/google/a2a)

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
- Sidecar and initcontainer YAML generation in Helm chart templates (currently: analysis/planning only)
- Prometheus ServiceMonitor/PodMonitor resource generation (currently: monitoring labels/annotations only)

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

📖 **[Supervisor Agent Documentation](./docs/supervisor/README.md)**

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

📖 **[Template Coordinator Documentation](./docs/template/README.md)**

#### 4. **Generator Agent (Validator)**
Deep agent that validates, self-heals, and ensures charts are production-ready.

**Key Features**:
- Helm validation (lint, template, dry-run)
- Autonomous error fixing
- Retry logic with human escalation
- Workspace file management

📖 **[Generator Agent Documentation](./docs/generator/README.md)**

---

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- Helm CLI installed and in PATH (required for local usage; not needed if running via Docker image)
- LLM API key (OpenAI, Anthropic)
- TalkOps client installed: `pip install talkops-client` (or `uv pip install talkops-client` if using uv)

### Installation

#### Option 1: Standalone Installation

1. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)** for dependency management

2. **Create and activate a virtual environment with Python 3.12:**

   ```sh
   uv venv --python=3.12
   source .venv/bin/activate  # On Unix/macOS
   # or
   .venv\Scripts\activate  # On Windows
   ```

3. **Install dependencies from pyproject.toml:**

   ```sh
   uv pip install -e .
   ```

4. **Create a `.env` file and add the following environment variables:**

   ```sh
   OPENAI_API_KEY=XXXXXXXXX
   ```

   > **Note:** All available configuration options can be found in [`k8s_autopilot/config/default.py`](k8s_autopilot/config/default.py). You can set any of these options via your `.env` file to customize the k8s-autopilot agent's behavior.

5. **Start the A2A server with the agent card:**

   ```sh
   uv run --active k8s-autopilot \
     --host localhost \
     --port 10102 \
     --agent-card k8s_autopilot/card/k8s_autopilot.json
   ```

#### Option 2: Docker Hub (Recommended for Quick Start)

**Pull and run the pre-built image from Docker Hub:**

```bash
# Pull the latest version
docker pull sandeep2014/k8s-autopilot:latest

# Run the A2A server
docker run -d -p 10102:10102 \
  -e OPENAI_API_KEY=your_openai_key_here \
  --name k8s-autopilot \
  sandeep2014/k8s-autopilot:latest
```

> **Note:** It's recommended to mount a local volume to persist generated Helm charts. By default, the agent writes charts to `/tmp/helm-charts` inside the container. You can mount a local directory and optionally configure the agent to write to a different directory:
>
> ```bash
> docker run -d -p 10102:10102 \
>   -e OPENAI_API_KEY=your_openai_key_here \
>   -v /path/to/local/charts:/tmp/helm-charts \
>   --name k8s-autopilot \
>   sandeep2014/k8s-autopilot:latest
> ```
>
> This allows you to access the generated Helm charts from your host machine and use them to install applications on your Kubernetes cluster.

**Available Docker tags:**

- `latest` - Latest stable release
- `v0.1.0` - Specific version

### Working with Agents

Once you have the agent running (using either Option 1 or Option 2 above), you can interact with it using the TalkOps client:

1. **Start the agent** using one of the installation methods above

2. **Connect to the agent** using the TalkOps client:

   ```bash
   talkops
   ```

   > **Note:** Make sure you have installed the TalkOps client: `pip install talkops-client` (or `uv pip install talkops-client` if using uv)

3. **Ask questions** about creating Helm charts for any application:

   ```
   Create a Helm chart for nginx
   ```

   or

   ```
   Generate a Helm chart for a Node.js application
   ```

   The agent will guide you through the process, asking for clarifications if needed, and generate production-ready Helm charts.

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
- **Traefik CRDs**: Modern ingress routing

### State Management

- **PostgreSQL Checkpointer**: Persistent state storage (preferred)
- **MemorySaver**: In-memory checkpointing (fallback)
- **State Reducers**: Concurrent update handling

### LLM Providers

- **OpenAI**: GPT-4, GPT-3.5-turbo
- **Anthropic**: Claude Sonnet, Claude Opus
- **Configurable**: Via centralized `LLMProvider`

### LLM Configuration

#### Multi-Provider Architecture

The system uses a modular, extensible LLM provider architecture built on an abstract base class pattern:

- **Multi-Provider Support**: Support for multiple LLM providers (Anthropic, OpenAI, Azure, etc.)
- **Model Selection**: Configurable model selection per agent
- **Parameter Tuning**: Adjustable temperature, max tokens, and other parameters
- **Provider Switching**: Easy switching between different LLM providers

#### Adding a New LLM Provider

To add a new LLM provider to the system, follow these steps:

##### 1. Implement the Provider Class

**Location:** `k8s_autopilot/core/llm/llm_provider.py`

Create a new provider class that inherits from `BaseLLMProvider`:

```python
from .base_llm_provider import BaseLLMProvider
from langchain_core.runnables import Runnable

class MyNewProvider(BaseLLMProvider):
    """
    Concrete LLM provider for MyNewProvider models.
    Implements the BaseLLMProvider interface.
    """
    def create_llm(
        self,
        model: str,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        timeout: int = 60,
        **kwargs: Any
    ) -> Runnable:
        # Import your provider's SDK or LangChain integration
        # from my_provider_sdk import MyProviderLLM
        
        # Build configuration dictionary
        config = {
            "model": model,
            "temperature": temperature,
            # ... other parameters ...
        }
        config.update(kwargs)
        
        # Return a LangChain-compatible Runnable
        return MyProviderLLM(**config)
```

##### 2. Register in the Factory

Update the `_create_provider_instance` method in `LLMProvider`:

```python
elif provider == "mynewprovider":
    return MyNewProvider().create_llm(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        **kwargs
    )
```

Add your provider to the `_SUPPORTED_PROVIDERS` set.

##### 3. Add Environment Variables (Optional)

If your provider requires API keys or endpoints, add them to your `.env` file:

```bash
MY_NEW_PROVIDER_API_KEY=your_api_key_here
MY_NEW_PROVIDER_ENDPOINT=https://api.example.com
```

##### 4. Test Your Provider

Ensure your provider works by running the agent with your provider selected in the configuration.

#### Best Practices for Provider Implementation

- Follow the structure and docstring style of existing providers
- Use type hints and clear error messages
- Keep all provider-specific logic encapsulated in your provider class
- Do not modify agent or planner code—only the provider and factory
- Handle API keys and endpoints securely

#### Reference Documentation

- [Base LLM Provider](k8s_autopilot/core/llm/base_llm_provider.py)
- [LLM Provider Factory](k8s_autopilot/core/llm/llm_provider.py)
- [Complete Onboarding Guide](docs/ONBOARDING_LLM_PROVIDER.md)

---

## 📋 Roadmap

### Current Release (v0.1.0)

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

**v0.2.0 - Enhanced Validation** (Planned):
- [ ] Security scanning integration (Kubesec, Trivy)
- [ ] Policy compliance checking
- [ ] Helm unit test generation
- [ ] ArgoCD Application manifest generation
- [ ] Traefik IngressRoute improvements and enhanced features

**v0.3.0 - Deployment Automation** (Planned):
- [ ] Automated deployment to Kubernetes clusters
- [ ] Deployment rollback capabilities
- [ ] Multi-cluster deployment strategies
- [ ] Deployment status monitoring

**v0.4.0 - CI/CD Integration** (Planned):
- [ ] GitHub Actions workflow generation
- [ ] GitLab CI pipeline generation
- [ ] Jenkins pipeline generation

**v0.5.0 - Testing & Evaluation** (Planned):
- [ ] Test case generation for Helm charts
- [ ] Evaluation framework integration
- [ ] Automated testing integration
- [ ] Chart quality metrics and scoring

**v0.6.0 - Extended Capabilities** (Planned):
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

## 🤝 Contributing

We welcome contributions! Please see below details.

### Development Setup

```bash
# Clone repository
git clone https://github.com/talkops-ai/k8s-autopilot.git
cd k8s-autopilot

# Install uv (if not already installed)
# See: https://docs.astral.sh/uv/getting-started/installation/

# Create virtual environment with Python 3.12
uv venv --python=3.12
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install development dependencies
uv pip install -e .

```

### Development Workflow

1. **Fork the repository**
2. **Create a feature branch** (`git checkout -b feature/amazing-feature`)
3. **Commit your changes** (`git commit -m 'Add amazing feature'`)
4. **Push to the branch** (`git push origin feature/amazing-feature`)
5. **Open a Pull Request**

### Contributing Guidelines

- Follow the existing code style and conventions
- Add tests for new features
- Update documentation as needed
- Ensure all tests pass before submitting PR
- Write clear, descriptive commit messages

### Code Style

- **Formatting**: Black
- **Linting**: Ruff
- **Type Checking**: mypy
- **Documentation**: Google-style docstrings

---

## 📝 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **LangChain Team**: For the excellent LangChain and LangGraph frameworks
- **Google A2A Protocol**: For the Agent-to-Agent communication protocol enabling enterprise agent ecosystems
- **CNCF**: For Kubernetes and Helm community standards
- **Traefik**: For modern ingress routing capabilities

---

## 💬 Community & Support

Join our community to get help, share ideas, and contribute to the project!

### Discord Community

**Join our Discord**

Connect with other users, developers, and contributors:

- 💡 **Get Help**: Ask questions and get support from the community
- 🚀 **Share Projects**: Showcase your Helm charts and use cases
- 🐛 **Report Issues**: Discuss bugs and feature requests
- 🤝 **Collaborate**: Find contributors and collaborate on improvements
- 📢 **Stay Updated**: Get notified about new releases and updates

[Join the Discord Server →](https://discord.gg/hFt5DAYEVx)

### Getting Support

- **Discord**: For real-time help and discussions, join our Discord community
- **GitHub Issues**: For bug reports and feature requests, use [GitHub Issues](https://github.com/talkops-ai/k8s-autopilot/issues)
- **Documentation**: Check our comprehensive [documentation](./docs/) for guides and references

---

## 🌟 Star Us

If you find k8s-autopilot helpful, please consider starring our repository:

⭐ [https://github.com/talkops-ai/k8s-autopilot](https://github.com/talkops-ai/k8s-autopilot)


---

**Built with ❤️ for the DevOps and Infrastructure community**
