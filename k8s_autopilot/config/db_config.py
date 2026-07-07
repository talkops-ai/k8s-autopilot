import asyncio
import json
import os
from typing import Any
import psycopg
from psycopg.rows import dict_row

from k8s_autopilot.config.default import DefaultConfig
from k8s_autopilot.utils.logger import AgentLogger

db_logger = AgentLogger("k8sAutopilotDbConfig")

SETTINGS_METADATA = {
    "GOOGLE_API_KEY": {
        "display_name": "Google Gemini API Key",
        "description": "API Key used to authenticate requests to Gemini and Vertex AI models."
    },
    "LLM_PROVIDER": {
        "display_name": "Standard LLM Provider",
        "description": "The default language model provider. Supported values: 'google_genai', 'openai', 'anthropic', 'azure_openai', 'ollama'."
    },
    "LLM_MODEL": {
        "display_name": "Standard LLM Model",
        "description": "Model used for fast validation and formatting tasks (e.g. 'gemini-3.1-flash-lite-preview')."
    },
    "LLM_TEMPERATURE": {
        "display_name": "Standard Model Temperature",
        "description": "Controls response randomness. Lower values (like 0.0) produce deterministic outputs."
    },
    "LLM_MAX_TOKENS": {
        "display_name": "Standard Model Max Tokens",
        "description": "Max tokens generated in standard LLM responses."
    },
    "LLM_THINKING_ENABLED": {
        "display_name": "Standard Model Thinking Mode",
        "description": "Enables reasoning/thinking tokens for supported models."
    },
    "LLM_THINKING_BUDGET": {
        "display_name": "Standard Model Thinking Budget",
        "description": "Token budget allocated for standard model reasoning."
    },
    "LLM_HIGHER_PROVIDER": {
        "display_name": "Supervisor LLM Provider",
        "description": "Model provider used for high-level agent routing."
    },
    "LLM_HIGHER_MODEL": {
        "display_name": "Supervisor LLM Model",
        "description": "Model used by the Supervisor for plan generation and intent parsing."
    },
    "LLM_HIGHER_TEMPERATURE": {
        "display_name": "Supervisor Model Temperature",
        "description": "Controls response randomness for the Supervisor model."
    },
    "LLM_HIGHER_MAX_TOKENS": {
        "display_name": "Supervisor Model Max Tokens",
        "description": "Max tokens generated in Supervisor responses."
    },
    "LLM_HIGHER_THINKING_ENABLED": {
        "display_name": "Supervisor Model Thinking Mode",
        "description": "Enables reasoning/thinking tokens for the Supervisor model."
    },
    "LLM_HIGHER_THINKING_BUDGET": {
        "display_name": "Supervisor Model Thinking Budget",
        "description": "Token budget allocated for Supervisor model reasoning."
    },
    "LLM_DEEPAGENT_PROVIDER": {
        "display_name": "DeepAgent LLM Provider",
        "description": "Model provider used for deep execution planning and coding."
    },
    "LLM_DEEPAGENT_MODEL": {
        "display_name": "DeepAgent LLM Model",
        "description": "Model used by coordinators for multi-step task execution."
    },
    "LLM_DEEPAGENT_TEMPERATURE": {
        "display_name": "DeepAgent Model Temperature",
        "description": "Controls response randomness for DeepAgent reasoning tasks."
    },
    "LLM_DEEPAGENT_MAX_TOKENS": {
        "display_name": "DeepAgent Model Max Tokens",
        "description": "Max tokens generated in DeepAgent coordinator responses."
    },
    "LLM_DEEPAGENT_THINKING_ENABLED": {
        "display_name": "DeepAgent Model Thinking Mode",
        "description": "Enables reasoning/thinking tokens for the DeepAgent model."
    },
    "LLM_DEEPAGENT_THINKING_BUDGET": {
        "display_name": "DeepAgent Model Thinking Budget",
        "description": "Token budget allocated for DeepAgent model reasoning."
    },
    "SLACK_BOT_TOKEN": {
        "display_name": "Slack Bot OAuth Token",
        "description": "Token used to connect the agent to your Slack workspace (starts with xoxb-)."
    },
    "SLACK_SIGNING_SECRET": {
        "display_name": "Slack Signing Secret",
        "description": "Secret key used to verify the integrity of incoming Slack webhooks."
    },
    "SLACK_ENABLED": {
        "display_name": "Enable Slack Integration",
        "description": "Master switch to activate or deactivate the Slack ChatOps server."
    },
    "GITHUB_PERSONAL_ACCESS_TOKEN": {
        "display_name": "GitHub Access Token",
        "description": "PAT used by the GitHub MCP server to commit charts to repositories."
    },
    "ARGOCD_SERVER_URL": {
        "display_name": "ArgoCD Server URL",
        "description": "Endpoint URL of the target ArgoCD server (e.g. https://localhost:8080)."
    },
    "ARGOCD_USERNAME": {
        "display_name": "ArgoCD Username",
        "description": "Username used to authenticate with ArgoCD (e.g. admin)."
    },
    "ARGOCD_PASSWORD": {
        "display_name": "ArgoCD Password",
        "description": "Password used to authenticate with ArgoCD."
    },
    "ARGOCD_AUTH_TOKEN": {
        "display_name": "ArgoCD Auth Token",
        "description": "Access token used by the ArgoCD MCP server to interact with APIs."
    },
    "ARGOCD_INSECURE": {
        "display_name": "ArgoCD Insecure SSL Check",
        "description": "Allows connecting to ArgoCD URL without verifying SSL/TLS certificates."
    },
    "PROMETHEUS_BASE_URL": {
        "display_name": "Prometheus Server URL",
        "description": "Base URL of Prometheus metric server queried by the Prometheus MCP."
    },
    "ALERTMANAGER_BASE_URL": {
        "display_name": "Alertmanager Server URL",
        "description": "Base URL of Alertmanager service queried by the Alertmanager MCP."
    },
    "LOKI_URL": {
        "display_name": "Loki Endpoint URL",
        "description": "Base URL of Loki log collector queried by the Loki MCP."
    },
    "TEMPO_BASE_URL": {
        "display_name": "Tempo Endpoint URL",
        "description": "Base URL of Tempo trace collector queried by the Tempo MCP."
    },
    "LOG_LEVEL": {
        "display_name": "System Logging Level",
        "description": "Controls application logs verbosity ('DEBUG', 'INFO', 'WARNING', 'ERROR')."
    },
    "AUTOPILOT_MODE": {
        "display_name": "Autopilot Execution Mode",
        "description": "Execution target environment: 'a2a' (HTTP API), 'slack' (ChatOps), or 'dual'."
    },
    "HELM_WORKSPACE": {
        "display_name": "Helm Workspace Path",
        "description": "Directory path on local disk where Helm charts are generated and tested."
    },
    "GOOGLE_API_KEY": {
        "display_name": "Google API Key",
        "description": "API key for Gemini models via Vertex AI or Google Gen AI."
    },
    "GOOGLE_GENAI_USE_VERTEXAI": {
        "display_name": "Use Vertex AI",
        "description": "Flag to switch to Google Vertex AI environment instead of direct Gemini API."
    },
    "LANGCHAIN_API_KEY": {
        "display_name": "LangSmith API Key",
        "description": "API Token used to authenticate with LangSmith tracing backend."
    },
    "LANGCHAIN_TRACING_V2": {
        "display_name": "Enable LangSmith Tracing",
        "description": "Enables tracing of LLM chains, agent flows, and runs using LangSmith."
    },
    "LANGCHAIN_PROJECT": {
        "display_name": "LangSmith Project Name",
        "description": "The project identifier in LangSmith where tracing runs will be organized."
    },
    "LANGGRAPH_STRICT_MSGPACK": {
        "display_name": "Strict MsgPack Serialization",
        "description": "Enables strict message packaging serialization for LangGraph state persistence."
    },
    "OPENAI_API_KEY": {
        "display_name": "OpenAI API Key",
        "description": "API token used to authenticate requests to OpenAI API."
    },
    "ANTHROPIC_API_KEY": {
        "display_name": "Anthropic API Key",
        "description": "API token used to authenticate requests to Anthropic (Claude) API."
    },
    "AZURE_OPENAI_API_KEY": {
        "display_name": "Azure OpenAI API Key",
        "description": "API key used to authenticate with Azure OpenAI service instance."
    },
    "AZURE_OPENAI_ENDPOINT": {
        "display_name": "Azure OpenAI Endpoint URL",
        "description": "Endpoint URL of your deployed Azure OpenAI resource instance."
    },
    "OPENAI_API_VERSION": {
        "display_name": "Azure OpenAI API Version",
        "description": "Version of the OpenAI API used by your Azure OpenAI service instance."
    },
    "AWS_ACCESS_KEY_ID": {
        "display_name": "AWS Access Key ID",
        "description": "AWS access credential ID used to authenticate requests to Bedrock API."
    },
    "AWS_SECRET_ACCESS_KEY": {
        "display_name": "AWS Secret Access Key",
        "description": "AWS secret access credential used to authenticate requests to Bedrock API."
    },
    "AWS_DEFAULT_REGION": {
        "display_name": "AWS Default Region",
        "description": "Target AWS region where your Bedrock model deployments reside."
    },
    "OTEL_CRD_GROUP": {
        "display_name": "OTel Operator CRD Group",
        "description": "API group name of the OpenTelemetry Operator CRD (default: 'opentelemetry.io')."
    },
    "OTEL_CRD_API_VERSION": {
        "display_name": "OTel Operator CRD API Version",
        "description": "API version of the OpenTelemetry Operator CRD (default: 'v1beta1')."
    },
    "OTEL_INSTRUMENTATION_API_VERSION": {
        "display_name": "OTel Instrumentation API Version",
        "description": "API version used for OpenTelemetry Instrumentations (default: 'v1alpha1')."
    },
    "OTEL_COLLECTOR_PLURAL": {
        "display_name": "OTel Collector Plural Name",
        "description": "Plural resource name of OpenTelemetry Collectors (default: 'opentelemetrycollectors')."
    },
    "OTEL_INSTRUMENTATION_PLURAL": {
        "display_name": "OTel Instrumentation Plural Name",
        "description": "Plural resource name of OpenTelemetry Instrumentations (default: 'instrumentations')."
    },
    "OTEL_TA_SERVICE_DISCOVERY": {
        "display_name": "OTel Target Allocator Service Discovery",
        "description": "Enables Prometheus service discovery via Target Allocator (default: true)."
    },
    "OTEL_TA_DEFAULT_PORT": {
        "display_name": "OTel Target Allocator Default Port",
        "description": "Default port used by the Target Allocator service (default: 8080)."
    },
    "TEMPO_BACKEND_ID": {
        "display_name": "Tempo Backend ID",
        "description": "Identifier for the single backend deployment (default: 'default')."
    },
    "TEMPO_DISPLAY_NAME": {
        "display_name": "Tempo Display Name",
        "description": "Human-readable label for this Tempo service (default: 'Local Tempo')."
    },
    "TEMPO_TYPE": {
        "display_name": "Tempo Server Type",
        "description": "Underlying server architecture Type (tempo | tempo-gateway | unknown)."
    },
    "TEMPO_DEPLOYMENT_MODE": {
        "display_name": "Tempo Deployment Mode",
        "description": "Tempo cluster deployment architecture (monolithic | microservices | unknown)."
    },
    "TEMPO_AUTH_HEADER": {
        "display_name": "Tempo Auth Header",
        "description": "Custom HTTP authorization headers for querying Tempo."
    },
    "TEMPO_VERIFY_SSL": {
        "display_name": "Tempo Verify SSL",
        "description": "Enables or disables SSL/TLS certificate verification for Tempo endpoint queries."
    },
    "TEMPO_TIMEOUT": {
        "display_name": "Tempo Query Timeout",
        "description": "Timeout in seconds for trace query requests to Tempo."
    },
    "TEMPO_MULTI_TENANT": {
        "display_name": "Tempo Multi-Tenancy Mode",
        "description": "Enables multi-tenant trace isolation inside Tempo (default: false)."
    },
    "TEMPO_DEFAULT_TENANT": {
        "display_name": "Tempo Default Tenant",
        "description": "Default tenant identifier header sent with Tempo queries."
    },
    "TEMPO_BACKENDS": {
        "display_name": "Tempo Multi-Backends Configuration",
        "description": "JSON array of Tempo backends for multi-backend deployments."
    },
    "TEMPO_MAX_LOOKBACK": {
        "display_name": "Tempo Max Lookback Window",
        "description": "Maximum trace age search window (default: '168h' / 7 days)."
    },
    "TEMPO_DEFAULT_SEARCH_LIMIT": {
        "display_name": "Tempo Default Search Limit",
        "description": "Default max trace results returned per search query."
    },
    "TEMPO_MAX_SEARCH_LIMIT": {
        "display_name": "Tempo Max Search Limit",
        "description": "Maximum allowed trace search result limits."
    },
    "TEMPO_DEFAULT_SPSS": {
        "display_name": "Tempo Default SPSS",
        "description": "Default spans-per-second search limit (default: 3)."
    },
    "TEMPO_MAX_SPSS": {
        "display_name": "Tempo Max SPSS",
        "description": "Maximum allowed spans-per-second limit."
    },
    "TEMPO_REQUIRE_TIME_RANGE": {
        "display_name": "Tempo Require Time Range",
        "description": "Force time-range constraints on all trace search queries."
    },
    "TEMPO_REQUIRE_FILTER_OR_QUERY": {
        "display_name": "Tempo Require Query Filter",
        "description": "Block empty trace queries without minimum filter configurations."
    },
    "LOKI_TIMEOUT": {
        "display_name": "Loki Query Timeout",
        "description": "Timeout in seconds for query requests sent to Loki."
    },
    "LOKI_VERIFY_SSL": {
        "display_name": "Loki Verify SSL",
        "description": "Enables or disables SSL/TLS certificate verification for Loki endpoint queries."
    },
    "LOKI_AUTH_TOKEN": {
        "display_name": "Loki Authorization Token",
        "description": "Authorization token (e.g. Bearer token) for querying Loki endpoints."
    },
    "LOKI_BASIC_AUTH_USER": {
        "display_name": "Loki Basic Auth Username",
        "description": "Username used for HTTP Basic authentication with Loki."
    },
    "LOKI_BASIC_AUTH_PASSWORD": {
        "display_name": "Loki Basic Auth Password",
        "description": "Password used for HTTP Basic authentication with Loki."
    },
    "LOKI_ORG_ID": {
        "display_name": "Loki Tenant Organization ID",
        "description": "Loki tenant Org ID header sent with requests (default: 'talkops')."
    },
    "LOKI_MAX_QUERY_BYTES": {
        "display_name": "Loki Max Query Bytes",
        "description": "Maximum volume limit of query log bytes to scan (default: 5000000000 / 5GB)."
    },
    "LOKI_MAX_TIME_WINDOW_HOURS": {
        "display_name": "Loki Max Query Time Window",
        "description": "Maximum time window constraint in hours for query intervals (default: 336 / 14 days)."
    },
    "LOKI_MAX_LOG_LIMIT": {
        "display_name": "Loki Max Log Limit",
        "description": "Maximum number of logs returned per query request (default: 5000)."
    },
    "LOKI_HIGH_CARDINALITY_THRESHOLD": {
        "display_name": "Loki High Cardinality Threshold",
        "description": "Cardinality limit warning trigger threshold (default: 10000)."
    }
}

def _determine_category(key: str) -> str:
    key_upper = key.upper()
    if key_upper.startswith("LLM_") or key_upper in (
        "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_VERSION",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"
    ):
        return "llm"
    if any(p in key_upper for p in ("LANGCHAIN_", "LANGGRAPH_")):
        return "langsmith"
    if any(p in key_upper for p in ("ARGOCD_", "GITHUB_", "HELM_", "PROMETHEUS_", "ALERTMANAGER_", "LOKI_", "TEMPO_", "OTEL_", "AM_")):
        return "mcp"
    if any(p in key_upper for p in ("SLACK_", "NGROK_", "A2A_SERVER_")):
        return "integration"
    return "system"

def _determine_mcp_server_id(key: str) -> str | None:
    key_upper = key.upper()
    if key_upper.startswith("ARGOCD_"):
        return "argocd"
    if key_upper.startswith("GITHUB_"):
        return "github"
    if key_upper.startswith("ALERTMANAGER_"):
        return "alertmanager"
    if key_upper.startswith("PROMETHEUS_"):
        return "prometheus"
    if key_upper.startswith("LOKI_"):
        return "loki"
    if key_upper.startswith("TEMPO_"):
        return "tempo"
    if key_upper.startswith("OTEL_"):
        return "otel"
    return None

def _determine_type(val: Any) -> str:
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, (list, dict)):
        return "json"
    return "str"

def _is_sensitive(key: str) -> bool:
    key_upper = key.upper()
    if any(k in key_upper for k in ("MAX_TOKENS", "BUDGET_TOKENS", "LIMIT")):
        return False
    return any(keyword in key_upper for keyword in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "URI"))

def serialize_value(val: Any, type_str: str) -> str:
    if val is None:
        return ""
    if type_str == "bool":
        return "true" if val else "false"
    if type_str == "json":
        return json.dumps(val)
    return str(val)

def deserialize_value(val_str: str, type_str: str) -> Any:
    if val_str == "" and type_str != "str":
        return None
    if type_str == "bool":
        return val_str.lower() in ("true", "1", "yes", "on")
    if type_str == "int":
        return int(val_str)
    if type_str == "float":
        return float(val_str)
    if type_str == "json":
        return json.loads(val_str)
    return val_str

async def init_settings_table(db_uri: str) -> None:
    """Initialize the settings table and triggers, and seed default values."""
    try:
        db_logger.info("Initializing Postgres settings table...")
        async with await psycopg.AsyncConnection.connect(db_uri, autocommit=True) as conn:
            async with conn.cursor() as cur:
                # Create table with CHECK constraints
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS k8s_autopilot_settings (
                        key VARCHAR(255) PRIMARY KEY,
                        value TEXT NOT NULL,
                        default_value TEXT,
                        type VARCHAR(50) NOT NULL CHECK (type IN ('str', 'int', 'float', 'bool', 'json')),
                        category VARCHAR(100) NOT NULL CHECK (category IN ('llm', 'integration', 'mcp', 'system', 'langsmith')),
                        mcp_server_id VARCHAR(100),
                        is_sensitive BOOLEAN DEFAULT FALSE,
                        is_editable BOOLEAN DEFAULT TRUE,
                        display_name VARCHAR(255),
                        description TEXT,
                        updated_by VARCHAR(255) DEFAULT 'system',
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                # Migrations for existing schemas
                await cur.execute("ALTER TABLE k8s_autopilot_settings ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);")
                await cur.execute("ALTER TABLE k8s_autopilot_settings ADD COLUMN IF NOT EXISTS description TEXT;")
                await cur.execute("ALTER TABLE k8s_autopilot_settings ADD COLUMN IF NOT EXISTS mcp_server_id VARCHAR(100);")
                await cur.execute("ALTER TABLE k8s_autopilot_settings DROP CONSTRAINT IF EXISTS k8s_autopilot_settings_category_check;")
                await cur.execute("""
                    UPDATE k8s_autopilot_settings 
                    SET category = 'mcp' 
                    WHERE category = 'observability' 
                       OR LEFT(key, 7) = 'ARGOCD_' 
                       OR LEFT(key, 7) = 'GITHUB_' 
                       OR LEFT(key, 5) = 'HELM_' 
                       OR LEFT(key, 11) = 'PROMETHEUS_' 
                       OR LEFT(key, 5) = 'LOKI_' 
                       OR LEFT(key, 6) = 'TEMPO_' 
                       OR LEFT(key, 13) = 'ALERTMANAGER_';
                """)
                await cur.execute("""
                    ALTER TABLE k8s_autopilot_settings 
                    ADD CONSTRAINT k8s_autopilot_settings_category_check 
                    CHECK (category IN ('llm', 'integration', 'mcp', 'system', 'langsmith'));
                """)
                # Delete deprecated/removed config keys
                await cur.execute("DELETE FROM k8s_autopilot_settings WHERE key = 'SLACK_APP_TOKEN';")
                
                # Create notify function and trigger
                await cur.execute("""
                    CREATE OR REPLACE FUNCTION notify_settings_changed()
                    RETURNS TRIGGER AS $$
                    BEGIN
                        PERFORM pg_notify('settings_changed', COALESCE(NEW.key, OLD.key));
                        RETURN COALESCE(NEW, OLD);
                    END;
                    $$ LANGUAGE plpgsql;
                """)
                
                # Check trigger existence or drop & recreate
                await cur.execute("""
                    DROP TRIGGER IF EXISTS settings_changed_trigger ON k8s_autopilot_settings;
                    CREATE TRIGGER settings_changed_trigger
                    AFTER INSERT OR UPDATE OR DELETE ON k8s_autopilot_settings
                    FOR EACH ROW
                    EXECUTE FUNCTION notify_settings_changed();
                """)
                
                # Seed settings from DefaultConfig + environmental variables
                annotations = {}
                for cls in reversed(DefaultConfig.__mro__):
                    annotations.update(getattr(cls, "__annotations__", {}))

                for key in annotations:
                    if key.startswith("_"):
                        continue
                    
                    # Determine type, category, sensitive
                    default_val = getattr(DefaultConfig, key, None)
                    type_str = _determine_type(default_val)
                    category_str = _determine_category(key)
                    mcp_server_id = _determine_mcp_server_id(key)
                    sensitive_bool = _is_sensitive(key)
                    
                    # Read metadata
                    meta = SETTINGS_METADATA.get(key, {})
                    display_name = meta.get("display_name", key.replace("_", " ").title())
                    description = meta.get("description", f"Configuration for {key}")

                    # Read current env/dotenv value as initial value
                    env_val = os.getenv(key)
                    
                    if env_val is not None:
                        val_str = env_val
                    else:
                        val_str = serialize_value(default_val, type_str)
                        
                    default_val_str = serialize_value(default_val, type_str)
                    
                    # Insert if not exists
                    await cur.execute("""
                        INSERT INTO k8s_autopilot_settings (key, value, default_value, type, category, mcp_server_id, is_sensitive, is_editable, display_name, description, updated_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'system')
                        ON CONFLICT (key) DO UPDATE
                        SET category = EXCLUDED.category,
                            type = EXCLUDED.type,
                            mcp_server_id = EXCLUDED.mcp_server_id,
                            is_sensitive = EXCLUDED.is_sensitive,
                            display_name = EXCLUDED.display_name,
                            description = EXCLUDED.description;
                    """, (key, val_str, default_val_str, type_str, category_str, mcp_server_id, sensitive_bool, True, display_name, description))

                # Dynamically seed any environment variables loaded from .env/system that are custom MCP variables
                prefixes = ("ARGOCD_", "GITHUB_", "HELM_", "PROMETHEUS_", "ALERTMANAGER_", "LOKI_", "TEMPO_", "OTEL_")
                for env_key, env_val in os.environ.items():
                    if env_key.startswith(prefixes) and env_key not in annotations:
                        target_server = env_key.split("_")[0].lower()
                        category_str = "mcp"
                        sensitive_bool = _is_sensitive(env_key)
                        display_name = env_key.replace("_", " ").title()
                        description = f"Custom configuration for {env_key}"
                        
                        await cur.execute("""
                            INSERT INTO k8s_autopilot_settings (key, value, default_value, type, category, mcp_server_id, is_sensitive, is_editable, display_name, description, updated_by)
                            VALUES (%s, %s, NULL, 'str', %s, %s, %s, TRUE, %s, %s, 'system')
                            ON CONFLICT (key) DO UPDATE
                            SET mcp_server_id = EXCLUDED.mcp_server_id,
                                category = EXCLUDED.category,
                                is_sensitive = EXCLUDED.is_sensitive,
                                display_name = EXCLUDED.display_name,
                                description = EXCLUDED.description;
                        """, (env_key, str(env_val), category_str, target_server, sensitive_bool, display_name, description))
                    
        db_logger.info("Database settings table successfully initialized.")
    except Exception as e:
        db_logger.error(f"Error initializing settings table: {e}")

async def start_settings_listener(db_uri: str, config: Any) -> None:
    """LISTEN/NOTIFY background worker to reload config on changes."""
    db_logger.info("Starting settings listener background task...")
    while True:
        try:
            # Connect in autocommit mode so LISTEN works properly
            async with await psycopg.AsyncConnection.connect(db_uri, autocommit=True) as conn:
                db_logger.info("Settings listener connected to DB, issuing LISTEN settings_changed;")
                async with conn.cursor() as cur:
                    await cur.execute("LISTEN settings_changed;")
                
                # Perform an initial reload to sync on connection/reconnection
                await config.reload()
                
                # Loop to wait for notifies
                async for notify in conn.notifies():
                    db_logger.info(f"Notification received on channel '{notify.channel}' for key '{notify.payload}'")
                    await config.reload()
                    
        except asyncio.CancelledError:
            db_logger.info("Settings listener task cancelled.")
            break
        except Exception as e:
            db_logger.error(f"Settings listener connection lost: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)
