import json
import os
from typing import Dict, Any, List, Union, Type, get_origin, get_args
from k8s_autopilot.config.default import DefaultConfig
from k8s_autopilot.utils.exceptions import ConfigError
from dotenv import load_dotenv
# Load environment variables
load_dotenv()

class Config:
    """
    Configuration class for K8s Auto Pilot Agent.

    Precedence order for config values (highest to lowest):
    1. Runtime/programmatic overrides (via config dict parameter in __init__)
    2. Environment variables (from .env file or system environment)
    3. Defaults from DefaultConfig class

    All config keys are available as attributes and in the internal _config dict.
    
    Example:
        # Default from DefaultConfig
        config = Config()
        assert config.llm_provider == "openai"  # From DefaultConfig
        
        # Override with environment variable
        os.environ["LLM_PROVIDER"] = "anthropic"
        config = Config()
        assert config.llm_provider == "anthropic"  # From env var
        
        # Override with runtime config (highest precedence)
        config = Config({"LLM_PROVIDER": "azure_openai"})
        assert config.llm_provider == "azure_openai"  # From runtime config
    """

    def __init__(self, config: Dict[str, Any] = {}) -> None:
        """
        Initialize the configuration.
        
        Precedence order (highest to lowest):
        1. Runtime/programmatic overrides (via config dict parameter)
        2. Environment variables (from .env or system env)
        3. Defaults from DefaultConfig
        
        Args:
            config: Optional configuration dictionary to override defaults (highest precedence)
        """
        # Step 1: Start with default configuration
        default_config = {
            key: getattr(DefaultConfig, key) 
            for key in dir(DefaultConfig) 
            if not key.startswith('_') and not callable(getattr(DefaultConfig, key))
        }
        
        # Step 2: Override defaults with environment variables
        # Check ALL DefaultConfig keys for environment variables
        self._config = {}
        annotations = getattr(DefaultConfig, '__annotations__', {})
        
        for key in default_config.keys():
            default_value = default_config[key]
            env_value = os.getenv(key)
            
            if env_value is not None and env_value.strip():
                # Environment variable exists and is not empty - convert and use it
                type_hint = annotations.get(key, type(default_value))
                try:
                    value = self.convert_env_value(key, env_value, type_hint)
                except Exception as e:
                    raise ConfigError(
                        f"Failed to convert environment variable '{key}'={env_value!r} to {type_hint}: {e}"
                    )
            else:
                # No env var or empty - use default
                value = default_value
            
            self._config[key] = value
        
        # Step 2b: Also check for optional config keys that might not be in DefaultConfig
        # (e.g., AZURE_OPENAI_DEPLOYMENT_NAME)
        optional_keys = ['AZURE_OPENAI_DEPLOYMENT_NAME']
        for key in optional_keys:
            env_value = os.getenv(key)
            if env_value is not None and env_value.strip():
                # Store as string (can be converted later if needed)
                self._config[key] = env_value
        
        # Step 3: Override with runtime/programmatic config (highest precedence)
        self._config.update(config)
        
        # Step 4: Set attributes for easy access
        self._set_attributes()
    

    def _set_attributes(self) -> None:
        """
        Set attributes from the internal _config dict.
        This allows attribute-style access (e.g., config.llm_provider).
        """
        for key, value in self._config.items():
            setattr(self, key.lower(), value)

    def __getattr__(self, item: str) -> Any:
        """
        Allow attribute-style access to config keys.
        Raises AttributeError if the key is missing.
        """
        if item in self._config:
            return self._config[item]
        raise AttributeError(f"'Config' object has no attribute '{item}'")

    @property
    def llm_config(self) -> Dict[str, Any]:
        """
        Get the standard LLM configuration compatible with init_chat_model.
        
        Returns:
            Dictionary with model configuration. Format depends on provider:
            - OpenAI/Anthropic: {'model': 'model-name', 'model_provider': 'provider', ...}
            - Azure OpenAI: {'model': 'azure_openai:model-name', 'azure_deployment': '...', ...}
            - Google Gemini: {'model': 'google_genai:model-name', ...}
            - AWS Bedrock: {'model': 'model-name', 'model_provider': 'bedrock_converse', ...}
        """
        provider = self._config.get('LLM_PROVIDER', 'openai')
        model = self._config.get('LLM_MODEL', 'gpt-4o-mini')
        
        config: Dict[str, Any] = {
            'temperature': self._config.get('LLM_TEMPERATURE', 0.0),
            'max_tokens': self._config.get('LLM_MAX_TOKENS', 1000)
        }
        
        # Handle provider-specific model naming for init_chat_model compatibility
        if provider == 'azure_openai':
            # Azure uses special syntax: "azure_openai:model-name"
            config['model'] = f"azure_openai:{model}"
            # Azure-specific parameters
            deployment = self._config.get('AZURE_OPENAI_DEPLOYMENT_NAME')
            if deployment:
                config['azure_deployment'] = deployment
        elif provider in ('google_genai', 'gemini'):
            # Google uses special syntax: "google_genai:model-name"
            config['model'] = f"google_genai:{model}"
        elif provider in ('bedrock', 'aws_bedrock'):
            # Bedrock uses model_provider
            config['model'] = model
            config['model_provider'] = 'bedrock_converse'
        else:
            # Standard providers (openai, anthropic) - auto-inferred but can specify
            config['model'] = model
            if provider:
                config['model_provider'] = provider
        
        # Keep 'provider' key for backward compatibility during migration
        config['provider'] = provider
        
        return config

    @property
    def llm_higher_config(self) -> Dict[str, Any]:
        """
        Get the higher-tier LLM configuration compatible with init_chat_model.
        
        Returns:
            Dictionary with model configuration. Format depends on provider.
        """
        provider = self._config.get('LLM_HIGHER_PROVIDER', 'openai')
        model = self._config.get('LLM_HIGHER_MODEL', 'gpt-5')
        
        config: Dict[str, Any] = {
            'temperature': self._config.get('LLM_HIGHER_TEMPERATURE', 1.0),
            'max_tokens': self._config.get('LLM_HIGHER_MAX_TOKENS', 12000)
        }
        
        # Handle provider-specific model naming
        if provider == 'azure_openai':
            config['model'] = f"azure_openai:{model}"
            deployment = self._config.get('AZURE_OPENAI_DEPLOYMENT_NAME')
            if deployment:
                config['azure_deployment'] = deployment
        elif provider in ('google_genai', 'gemini'):
            config['model'] = f"google_genai:{model}"
        elif provider in ('bedrock', 'aws_bedrock'):
            config['model'] = model
            config['model_provider'] = 'bedrock_converse'
        else:
            config['model'] = model
            if provider:
                config['model_provider'] = provider
        
        # Keep 'provider' key for backward compatibility
        config['provider'] = provider
        
        return config

    def get_llm_config(self) -> Dict[str, Any]:
        """Get standard LLM configuration.
        
        Returns:
            Standard LLM configuration dictionary
        """
        return self.llm_config

    def get_llm_higher_config(self) -> Dict[str, Any]:
        """Get higher-tier LLM configuration.
        
        Returns:
            Higher-tier LLM configuration dictionary
        """
        return self.llm_higher_config

    @property
    def llm_deepagent_config(self) -> Dict[str, Any]:
        """
        Get the DeepAgent LLM configuration compatible with init_chat_model.
        
        Returns:
            Dictionary with model configuration. Format depends on provider.
        """
        provider = self._config.get('LLM_DEEPAGENT_PROVIDER', 'openai')
        model = self._config.get('LLM_DEEPAGENT_MODEL', 'gpt-4o')
        
        config: Dict[str, Any] = {
            'temperature': self._config.get('LLM_DEEPAGENT_TEMPERATURE', 0.0),
            'max_tokens': self._config.get('LLM_DEEPAGENT_MAX_TOKENS', 12000)
        }
        
        # Handle provider-specific model naming
        if provider == 'azure_openai':
            config['model'] = f"azure_openai:{model}"
            deployment = self._config.get('AZURE_OPENAI_DEPLOYMENT_NAME')
            if deployment:
                config['azure_deployment'] = deployment
        elif provider in ('google_genai', 'gemini'):
            config['model'] = f"google_genai:{model}"
        elif provider in ('bedrock', 'aws_bedrock'):
            config['model'] = model
            config['model_provider'] = 'bedrock_converse'
        else:
            config['model'] = model
            if provider:
                config['model_provider'] = provider
        
        # Keep 'provider' key for backward compatibility
        config['provider'] = provider
        
        return config

    def get_llm_deepagent_config(self) -> Dict[str, Any]:
        """Get DeepAgent LLM configuration.
        
        Returns:
            DeepAgent LLM configuration dictionary
        """
        return self.llm_deepagent_config

    @property
    def helm_mcp_config(self) -> Dict[str, Any]:
        """Get Helm MCP server configuration."""
        return {
            'host': self._config.get('HELM_MCP_SERVER_HOST', 'localhost'),
            'port': self._config.get('HELM_MCP_SERVER_PORT', 10100),
            'transport': self._config.get('HELM_MCP_SERVER_TRANSPORT', 'sse'),
            'disabled': self._config.get('HELM_MCP_SERVER_DISABLED', False)
        }

    def set_llm_config(self, config: Dict[str, Any]) -> None:
        """Set the standard LLM configuration.
        
        Args:
            config: Standard LLM configuration dictionary
        """
        for key, value in config.items():
            if key == 'provider':
                self._config['LLM_PROVIDER'] = value
            elif key == 'model':
                self._config['LLM_MODEL'] = value
            elif key == 'temperature':
                self._config['LLM_TEMPERATURE'] = value
            elif key == 'max_tokens':
                self._config['LLM_MAX_TOKENS'] = value

    def set_llm_higher_config(self, config: Dict[str, Any]) -> None:
        """Set the higher-tier LLM configuration.
        
        Args:
            config: Higher-tier LLM configuration dictionary
        """
        for key, value in config.items():
            if key == 'provider':
                self._config['LLM_HIGHER_PROVIDER'] = value
            elif key == 'model':
                self._config['LLM_HIGHER_MODEL'] = value
            elif key == 'temperature':
                self._config['LLM_HIGHER_TEMPERATURE'] = value
            elif key == 'max_tokens':
                self._config['LLM_HIGHER_MAX_TOKENS'] = value

    @staticmethod
    def convert_env_value(key: str, env_value: str, type_hint: Type) -> Any:
        """Convert environment variable to the appropriate type.
        
        Args:
            key: Configuration key
            env_value: Environment variable value
            type_hint: Type hint for the value
            
        Returns:
            Converted value
        """
        origin = get_origin(type_hint)
        args = get_args(type_hint)

        if origin is Union:
            for arg in args:
                if arg is type(None):
                    if env_value.lower() in ("none", "null", ""):
                        return None
                else:
                    try:
                        return Config.convert_env_value(key, env_value, arg)
                    except Exception:
                        continue
            raise ConfigError(f"Cannot convert {env_value} to any of {args}")

        if type_hint is bool:
            return env_value.lower() in ("true", "1", "yes", "on")
        elif type_hint is int:
            return int(env_value)
        elif type_hint is float:
            return float(env_value)
        elif type_hint in (str, Any):
            return env_value
        elif origin is list or origin is List:
            return json.loads(env_value)
        else:
            raise ConfigError(f"Unsupported type {type_hint} for key {key}")

    @classmethod
    def load_config(cls, config_path: str) -> Dict[str, Any]:
        """Load configuration from file or use defaults.
        
        Args:
            config_path: Path to the configuration file
            
        Returns:
            Configuration dictionary
        """

        if not os.path.exists(config_path):
            print(f"Warning: Configuration not found at '{config_path}'. Using default configuration.")

        with open(config_path, "r") as f:
            custom_config = json.load(f)

        # Merge with default config
        merged_config = DefaultConfig.__dict__.copy()
        merged_config.update(custom_config)
        return merged_config