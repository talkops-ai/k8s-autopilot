# Model providers

> 20+ supported LLM providers with streaming, tool calling, and extended reasoning

k8s-autopilot works with 20+ model providers through LangChain's provider abstraction. You can switch models mid-conversation without restarting, and the agent automatically detects the right provider from the model name and available API keys.

## Model format

Models are referenced using `provider:model` format:

```
google_genai:gemini-3.7-flash
anthropic:claude-opus-4-7
openai:gpt-5.4
deepseek:deepseek-reasoner
ollama:llama3.1
```

You can set the model in several ways:

- **UI model picker:** Click the model selector in the chat interface to switch instantly
- **Settings UI:** Go to **Settings → Auth & Keys** to add provider keys, then select models in chat
- **Environment variable:** Set `MODEL=gemini-3.7-flash` in your `.env` file
- **Provider + model:** Set both `MODEL` and `MODEL_PROVIDER` for explicit control

> [!TIP]
> If you just set the model name without specifying a provider, k8s-autopilot auto-detects the provider. For example, `MODEL=gpt-4o` automatically routes to OpenAI, and `MODEL=gemini-3.7-flash` routes to Google GenAI.

## Supported providers

| Provider | ID | Auth env var | Highlights |
|---|---|---|---|
| **Google GenAI** | `google_genai` | `GOOGLE_API_KEY` | Gemini 3.7/3.6/3.5 Flash, Gemini 3.1 Pro, 1M context, Thinking |
| **Anthropic** | `anthropic` | `ANTHROPIC_API_KEY` | Claude 3.7 Sonnet, Claude Opus 4.7, Extended Thinking, Vision |
| **OpenAI** | `openai` | `OPENAI_API_KEY` | GPT-5.4, GPT-4o, o3-mini Reasoning, Vision |
| **Google Vertex AI** | `google_vertexai` | ADC (`GOOGLE_CLOUD_PROJECT`) | Enterprise Vertex AI endpoints |
| **Azure OpenAI** | `azure_openai` | `AZURE_OPENAI_API_KEY` | Azure-hosted OpenAI models |
| **Groq** | `groq` | `GROQ_API_KEY` | Ultra-low latency Llama 3.3 70B |
| **DeepSeek** | `deepseek` | `DEEPSEEK_API_KEY` | DeepSeek V3, R1 Reasoning |
| **Together AI** | `together` | `TOGETHER_API_KEY` | Open-source foundation models |
| **Fireworks AI** | `fireworks` | `FIREWORKS_API_KEY` | High-speed function calling |
| **OpenRouter** | `openrouter` | `OPENROUTER_API_KEY` | Multi-provider routing gateway (Claude, Gemini, Kimi K3) |
| **Mistral AI** | `mistralai` | `MISTRAL_API_KEY` | Mistral Large, Codestral, Pixtral |
| **NVIDIA NIM** | `nvidia` | `NVIDIA_API_KEY` | Accelerated NIM endpoints |
| **Perplexity** | `perplexity` | `PPLX_API_KEY` | Search-augmented Sonar models |
| **Cohere** | `cohere` | `COHERE_API_KEY` | Command R / R+ |
| **IBM watsonx** | `ibm` | `WATSONX_APIKEY` | Enterprise Granite and Llama |
| **HuggingFace** | `huggingface` | `HUGGINGFACEHUB_API_TOKEN` | Dedicated Inference Endpoints |
| **LiteLLM** | `litellm` | `LITELLM_API_KEY` | Unified proxy for internal gateways |
| **xAI** | `xai` | `XAI_API_KEY` | Grok 2 / Grok 3 |
| **Baseten** | `baseten` | `BASETEN_API_KEY` | Custom deployed models |
| **Ollama** | `ollama` | Optional | Fully local offline inference |

### Special cases

- **Google Vertex AI** and **AWS Bedrock** use implicit authentication (ADC / IAM roles). No explicit API key needed — just ensure the right cloud credentials are configured.
- **Ollama** doesn't require an API key at all. Just point to the Ollama server and go.

## Auto-detection

When you set `MODEL` without specifying `MODEL_PROVIDER`, the agent infers the provider from the model name:

| Model name prefix | Detected provider |
|---|---|
| `gpt-`, `o1`, `o3`, `o4`, `chatgpt` | `openai` |
| `claude-`, `sonnet`, `opus`, `haiku` | `anthropic` |
| `gemini` | `google_genai` |
| `deepseek` | `deepseek` |
| `llama` | `ollama` |

If the name doesn't match any prefix, the agent falls back to checking which providers have API keys configured, in this priority order: Google GenAI → Anthropic → OpenAI → Groq → DeepSeek → OpenRouter.

## Popular models

These are the models with detailed capability profiles built into k8s-autopilot:

| Model | Provider | Context | Reasoning | Tool calling |
|---|---|---|---|---|
| **Gemini 3.8 Flash** | `google_genai` / `google_vertexai` | 1M tokens | ✅ low/medium/high | ✅ |
| **Gemini 3.7 Flash** | `google_genai` / `google_vertexai` | 1M tokens | ✅ low/medium/high | ✅ |
| **Gemini 3.6 Flash** | `google_genai` | 1M tokens | ✅ low/medium/high | ✅ |
| **Gemini 3.5 Flash** | `google_genai` | 1M tokens | ✅ low/medium/high | ✅ |
| **Gemini 3.1 Pro** | `google_genai` | 1M tokens | ✅ low/high | ✅ |
| **Gemini 2.5 Pro** | `google_genai` | 1M tokens | ✅ low/medium/high | ✅ |
| **Claude Opus 4.7 / 4.8 / 5** | `anthropic` | 200K tokens | ✅ low/medium/high/max | ✅ |
| **Claude 3.7 Sonnet** | `anthropic` | 200K tokens | ✅ low/medium/high/max | ✅ |
| **Claude Sonnet 4.5 / 4.6 / 5** | `anthropic` | 200K tokens | ✅ low/medium/high/max | ✅ |
| **Claude Haiku 4.5** | `anthropic` | 200K tokens | — | ✅ |
| **Claude 3.5 Sonnet** | `anthropic` | 200K tokens | — | ✅ |
| **GPT-5.5 / 5.5 Pro** | `openai` | 400K tokens | ✅ low/medium/high | ✅ |
| **GPT-5.4 / 5.4 mini** | `openai` | 400K tokens | ✅ low/medium/high | ✅ |
| **o3 / o3-mini** | `openai` | 200K tokens | ✅ low/medium/high | ✅ |
| **GPT-4o / 4o mini** | `openai` | 128K tokens | — | ✅ |
| **DeepSeek V3** | `deepseek` | 64K tokens | — | ✅ |
| **DeepSeek R1** | `deepseek` | 64K tokens | ✅ | ✅ |
| **GLM 5.2** | `fireworks` / `baseten` / `openrouter` | 128K tokens | ✅ | ✅ |
| **Grok 4.5 / Grok 2** | `xai` | 131K tokens | ✅ | ✅ |
| **Llama 3.3 70B** | `groq` / `together` / `meta` | 128K tokens | — | ✅ |
| **Kimi K3 / K2.7** | `openrouter` / `baseten` / `fireworks` | 1M tokens | ✅ low/medium/high | ✅ |

Any model from any supported provider works — these are just the ones with optimized profiles. Unlisted models use sensible defaults.

## Extended thinking / reasoning

k8s-autopilot supports extended reasoning for models that have it. The reasoning effort controls how much "thinking" the model does before responding — higher effort means more thorough analysis but uses more tokens.

### Setting the effort

**From `.env`:**

```bash
REASONING_EFFORT=high    # Options: low, medium, high, max
```

**From the UI:** Go to **Settings → Runtime Config** to change the reasoning effort.

### How it works per provider

The `REASONING_EFFORT` setting is translated into each provider's native format:

| Provider | Native parameter | Example for `high` |
|---|---|---|
| **Google GenAI** | `thinking_level`, `thinking_budget` | `thinking_level="HIGH"`, `thinking_budget=8192` |
| **Anthropic** | `thinking.type`, `thinking.budget_tokens` | `thinking={"type": "enabled", "budget_tokens": 8192}` |
| **OpenAI** | `reasoning.effort` | `reasoning={"effort": "high"}` |

Not all models support all effort levels. Claude models support `max` effort while most others cap at `high`. The agent automatically clips to the nearest supported level.

## Switch models

### From the UI

The chat interface includes a model picker. Click it to see all available models grouped by provider. Only providers with configured API keys show their models — add a key in **Settings → Auth & Keys** to unlock more.

### From environment variables

```bash
# Set the default model
MODEL=gemini-3.7-flash

# Or be explicit about the provider
MODEL=gpt-5.4
MODEL_PROVIDER=openai

# Override the context limit (useful for testing)
MODEL_CONTEXT_LIMIT=32000
```

## Custom base URLs

For self-hosted, proxied, or enterprise deployments, you can override the API endpoint for any provider:

| Provider | Base URL variable(s) |
|---|---|
| **OpenAI** | `OPENAI_BASE_URL`, `OPENAI_API_BASE` |
| **Anthropic** | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_URL` |
| **Google GenAI** | `GOOGLE_GEMINI_BASE_URL` |
| **Azure OpenAI** | `AZURE_OPENAI_ENDPOINT` |
| **Groq** | `GROQ_BASE_URL`, `GROQ_API_BASE` |
| **DeepSeek** | `DEEPSEEK_API_BASE` |
| **Fireworks** | `FIREWORKS_BASE_URL`, `FIREWORKS_API_BASE` |
| **Together** | `TOGETHER_API_BASE` |
| **OpenRouter** | `OPENROUTER_API_BASE` |
| **Mistral** | `MISTRAL_BASE_URL` |
| **NVIDIA** | `NVIDIA_BASE_URL` |
| **Perplexity** | `PERPLEXITY_BASE_URL` |
| **xAI** | `XAI_API_BASE` |
| **Baseten** | `BASETEN_BASE_URL`, `BASETEN_API_BASE` |
| **Cohere** | `CO_API_URL` |
| **HuggingFace** | `HF_INFERENCE_ENDPOINT` |
| **IBM watsonx** | `WATSONX_URL` |

You can also set base URLs per-provider from the UI by clicking **Advanced Settings** in the API key dialog.

## Google Vertex AI

To use Google's enterprise Vertex AI instead of the consumer AI Studio API:

```bash
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
```

Authentication uses Application Default Credentials (ADC). Make sure you've run `gcloud auth application-default login` or have a service account key configured.

## Local models with Ollama

Run completely offline with no API keys:

```bash
# Pull a model
ollama pull llama3.1

# Set it as the active model
MODEL=llama3.1
MODEL_PROVIDER=ollama
```

By default, k8s-autopilot connects to Ollama at `http://localhost:11434`. To point to a different Ollama instance, set the `OLLAMA_HOST` environment variable or configure it in **Settings → Auth & Keys → Ollama → Advanced Settings**.

> [!NOTE]
> Local models vary in capability. Tool calling and structured output work best with larger models (13B+). Smaller models may struggle with complex multi-step operations.

## Next steps

- **[Configuration](./configuration.md)** — Full settings reference including all env vars.
- **[Quickstart](./quickstart.md)** — First-time setup and connecting your first provider.
