"""
LLM Provider Compatibility Module

This module provides compatibility functions for migrating from the old LLMProvider
to LangChain's init_chat_model. The old provider classes have been removed in favor
of using init_chat_model directly.

For new code, use init_chat_model directly:
    from langchain.chat_models import init_chat_model
    model = init_chat_model("gpt-4o")
"""

import os
from typing import Dict, Any
from langchain_core.runnables import Runnable
from k8s_autopilot.utils.exceptions import LLMConfigurationError


# Compatibility wrapper for migration to init_chat_model
# This function is kept for backward compatibility during migration period
def create_llm_with_init_chat_model(**kwargs: Any) -> Runnable:
    """
    Compatibility wrapper that uses LangChain's init_chat_model.
    
    This function maps the old LLMProvider.create_llm() API to init_chat_model(),
    supporting all providers: OpenAI, Anthropic, Azure OpenAI, Google Gemini, AWS Bedrock.
    
    Args:
        provider: LLM provider name ('openai', 'anthropic', 'azure_openai', 'google_genai', 'bedrock')
        model: Model name (e.g., 'gpt-4o', 'claude-3-opus-20240229')
        temperature: Sampling temperature (default: 0.1)
        max_tokens: Maximum tokens to generate
        timeout: Request timeout in seconds
        **kwargs: Additional provider-specific parameters
        
    Returns:
        Configured LangChain LLM instance (Runnable)
        
    Examples:
        >>> # OpenAI
        >>> model = create_llm_with_init_chat_model(provider='openai', model='gpt-4o')
        
        >>> # Anthropic
        >>> model = create_llm_with_init_chat_model(provider='anthropic', model='claude-3-opus-20240229')
        
        >>> # Azure OpenAI
        >>> model = create_llm_with_init_chat_model(
        ...     provider='azure_openai',
        ...     model='gpt-4.1',
        ...     azure_deployment='gpt-4-deployment'
        ... )
        
        >>> # Google Gemini
        >>> model = create_llm_with_init_chat_model(provider='google_genai', model='gemini-2.5-flash-lite')
        
        >>> # AWS Bedrock
        >>> model = create_llm_with_init_chat_model(
        ...     provider='bedrock',
        ...     model='anthropic.claude-3-5-sonnet-20240620-v1:0'
        ... )
    """
    try:
        from langchain.chat_models import init_chat_model
    except ImportError as e:
        raise LLMConfigurationError(
            f"init_chat_model not available. Ensure langchain>=0.2.7 is installed: {e}"
        )
    
    # Extract provider and model
    provider = kwargs.pop('provider', None)
    model = kwargs.get('model')
    
    if not model:
        raise LLMConfigurationError("Model name is required")
    
    # Build configuration for init_chat_model
    new_config: Dict[str, Any] = {}
    
    # Handle provider-specific model naming
    if provider == 'azure_openai':
        # Azure uses special syntax: "azure_openai:model-name"
        new_config['model'] = f"azure_openai:{model}"
        # Azure-specific parameters
        if 'endpoint' in kwargs:
            os.environ["AZURE_OPENAI_ENDPOINT"] = kwargs.pop('endpoint')
        if 'api_version' in kwargs:
            os.environ["OPENAI_API_VERSION"] = kwargs.pop('api_version')
        if 'deployment_name' in kwargs:
            new_config['azure_deployment'] = kwargs.pop('deployment_name')
        elif os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME'):
            new_config['azure_deployment'] = os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME')
    elif provider in ('google_genai', 'gemini'):
        # Google uses special syntax: "google_genai:model-name"
        new_config['model'] = f"google_genai:{model}"
    elif provider in ('bedrock', 'aws_bedrock'):
        # Bedrock uses model_provider
        new_config['model'] = model
        new_config['model_provider'] = 'bedrock_converse'
    else:
        # Standard providers (openai, anthropic) - auto-inferred
        new_config['model'] = model
        # Optionally specify model_provider for explicit control
        if provider:
            new_config['model_provider'] = provider
    
    # Pass through standard parameters
    for key in ['temperature', 'max_tokens', 'timeout']:
        if key in kwargs:
            new_config[key] = kwargs.pop(key)
    
    # Pass through any remaining kwargs (provider-specific parameters)
    new_config.update(kwargs)
    
    try:
        return init_chat_model(**new_config)
    except Exception as e:
        raise LLMConfigurationError(
            f"Failed to create LLM with init_chat_model for provider '{provider}': {e}"
        )
