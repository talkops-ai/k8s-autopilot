# Onboarding Guide: Configuring LLM Providers

This guide explains how to configure different LLM providers (OpenAI, Anthropic, Gemini, etc.) for `k8s-autopilot`.

## 🚀 Quick Start (Configuration)

Switching providers is simple. Just set the `LLM_PROVIDER` and `LLM_MODEL` environment variables in your `.env` file. The system automatically handles the connection details.

### 1. Anthropic (Claude)

```bash
# .env
LLM_PROVIDER="anthropic"
LLM_MODEL="claude-3-5-sonnet-20240620"
ANTHROPIC_API_KEY="sk-ant-..."
```

### 2. Google Gemini

```bash
# .env
LLM_PROVIDER="google_genai"
LLM_MODEL="gemini-1.5-pro"
GEMINI_API_KEY="AIza..."
```

### 3. OpenAI (Default)

```bash
# .env
LLM_PROVIDER="openai"
LLM_MODEL="gpt-4o"
OPENAI_API_KEY="sk-..."
```

---

## 📚 Provider Reference Table

Use these values in your `.env` file to configure specific providers.

| Provider | `LLM_PROVIDER` Value | Example `LLM_MODEL` Value | Required API Keys (Env Vars) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **OpenAI** | `openai` | `gpt-4o`, `gpt-4-turbo` | `OPENAI_API_KEY` | Default provider. |
| **Anthropic** | `anthropic` | `claude-3-5-sonnet-20240620` | `ANTHROPIC_API_KEY` | |
| **Google Gemini** | `google_genai` | `gemini-1.5-pro` | `GOOGLE_API_KEY` | Requires `langchain-google-genai` package. |
| **Azure OpenAI** | `azure_openai` | `gpt-4` | `AZURE_OPENAI_API_KEY`<br>`AZURE_OPENAI_ENDPOINT`<br>`OPENAI_API_VERSION` | Requires `AZURE_OPENAI_DEPLOYMENT_NAME` env var if different from model name. |
| **AWS Bedrock** | `bedrock` | `anthropic.claude-3-sonnet...` | `AWS_ACCESS_KEY_ID`<br>`AWS_SECRET_ACCESS_KEY`<br>`AWS_DEFAULT_REGION` | Uses standard AWS CLI/Boto3 credentials. |

---

### 🧠 Multi-Model Strategy: When to mix Providers?

The system is designed for a **Multi-Model** approach, allowing you to use different models for different roles to optimize for cost, speed, and reasoning capability.

#### 1. Supervisors (`LLM_DEEPAGENT_*`)
*   **Role**: Orchestration, complex state management, multi-step planning, tool selection.
*   **Recommended Models**: `o1-mini`, `gpt-4o`, `claude-3-opus`.
*   **Why?**: Supervisors need high reasoning capabilities to maintain conversation state, handle complex graph transitions, and ensure safety guardrails are met. They are called less frequently but make critical decisions.

#### 2. Workers / Sub-Agents (`LLM_*`)
*   **Role**: Specific narrow tasks (e.g., "Parse this JSON", "Generate this YAML", "Validate this schema").
*   **Recommended Models**: `gpt-4o-mini`, `gemini-1.5-flash`, `claude-3-haiku`.
*   **Why?**: These agents perform high-volume, repetitive tasks where speed and low cost are prioritized over deep reasoning. A "Flash" model is often perfect for validating a Kubernetes manifest against a schema.

**Example Scenario: Cost-Effective Enterprise Deployment**
*   **Supervisor**: Use **OpenAI o1-mini** (`LLM_DEEPAGENT_MODEL`) for robust planning and safety.
*   **Workers**: Use **Gemini 1.5 Flash** (`LLM_MODEL`) for blazing fast, nearly free code generation and validation loops.

```bash
# .env - Hybrid Configuration
LLM_DEEPAGENT_PROVIDER="openai"
LLM_DEEPAGENT_MODEL="o1-mini"

LLM_PROVIDER="google_genai"
LLM_MODEL="gemini-1.5-flash"
```

---

