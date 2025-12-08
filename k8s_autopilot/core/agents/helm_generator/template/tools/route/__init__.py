from typing import List, Dict, Any, Optional, Literal
from datetime import datetime

from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

# ============================================================================
# Input Schema
# ============================================================================

class IngressPath(BaseModel):
    path: str = Field(..., description="Path (e.g. /)")
    path_type: Literal["Prefix", "Exact", "ImplementationSpecific"] = Field("Prefix")
    service_name: str = Field(..., description="Backend service name")
    service_port: int = Field(..., description="Backend service port")

class IngressHost(BaseModel):
    host: str = Field(..., description="Hostname (e.g. app.example.com)")
    paths: List[IngressPath] = Field(..., min_items=1)

class IngressGenerationInput(BaseModel):
    """
    Input for Ingress generation.
    """
    app_name: str = Field(..., description="Application name")
    
    hosts: List[IngressHost] = Field(..., min_items=1)
    
    tls_enabled: bool = Field(True)
    tls_secret_name: Optional[str] = Field(None)
    
    ingress_class_name: Optional[str] = Field("nginx")
    
    annotations: Dict[str, str] = Field(default={})
    
    class Config:
        extra = "forbid"

# ============================================================================
# Output Schema
# ============================================================================

class IngressGenerationOutput(BaseModel):
    yaml_content: str = Field(..., description="Content of ingress.yaml")
    file_name: str = Field(default="ingress.yaml")
    validation_status: Literal["valid", "warning", "error"] = Field(...)
    validation_messages: List[str] = Field(default=[])
    metadata: Dict[str, Any] = Field(default={})

# ============================================================================
# Tool Implementation
# ============================================================================

INGRESS_GENERATOR_SYSTEM_PROMPT = """You are an expert Kubernetes Ingress generator.

## YOUR ROLE

Generate an Ingress manifest with Helm templating.

## TEMPLATE

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "CHARTNAME.fullname" . }}
  labels:
    {{- include "CHARTNAME.labels" . | nindent 4 }}
  {{- with .Values.ingress.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  {{- if .Values.ingress.className }}
  ingressClassName: {{ .Values.ingress.className }}
  {{- end }}
  {{- if .Values.ingress.tls }}
  tls:
    {{- range .Values.ingress.tls }}
    - hosts:
        {{- range .hosts }}
        - {{ . | quote }}
        {{- end }}
      secretName: {{ .secretName }}
    {{- end }}
  {{- end }}
  rules:
    {{- range .Values.ingress.hosts }}
    - host: {{ .host | quote }}
      http:
        paths:
          {{- range .paths }}
          - path: {{ .path }}
            pathType: {{ .pathType }}
            backend:
              service:
                name: {{ include "CHARTNAME.fullname" $ }}
                port:
                  number: {{ .port }}
          {{- end }}
    {{- end }}
```

## REQUIREMENTS

1. Use `networking.k8s.io/v1` API
2. Support TLS configuration
3. Use Helm values for hosts, paths, and annotations
4. Ensure service name matches the generated service (use fullname template)

## OUTPUT FORMAT

Return ONLY the YAML content.
"""

def create_ingress_user_prompt(input_data: IngressGenerationInput) -> str:
    return f"""Generate Ingress YAML for:
**App Name:** {input_data.app_name}
**Hosts:** {input_data.hosts}
**TLS Enabled:** {input_data.tls_enabled}
**Ingress Class:** {input_data.ingress_class_name}

Ensure proper Helm templating for all values.
"""

def extract_yaml_from_response(response: str) -> str:
    import re
    yaml_pattern = r'```yaml\n(.*?)\n```'
    match = re.search(yaml_pattern, response, re.DOTALL)
    if match:
        return match.group(1)
    return response.strip()

def validate_yaml_syntax(yaml_content: str) -> tuple[str, List[str]]:
    from ruamel.yaml import YAML
    yaml = YAML()
    try:
        yaml.load(yaml_content)
        return "valid", []
    except Exception as e:
        return "error", [str(e)]

@tool("generate_ingress_yaml", args_schema=IngressGenerationInput, return_direct=False)
def generate_ingress_yaml(input_data: IngressGenerationInput) -> IngressGenerationOutput:
    """
    Generates Ingress manifest.
    """
    
    llm = ChatOpenAI(
        model="gpt-4o",
        temperature=0.1,
        max_tokens=2048
    )
    
    response = llm.invoke([
        {"role": "system", "content": INGRESS_GENERATOR_SYSTEM_PROMPT},
        {"role": "user", "content": create_ingress_user_prompt(input_data)}
    ])
    
    yaml_content = extract_yaml_from_response(response.content)
    validation_status, validation_messages = validate_yaml_syntax(yaml_content)
    
    return IngressGenerationOutput(
        yaml_content=yaml_content,
        validation_status=validation_status,
        validation_messages=validation_messages,
        metadata={
            "model": "gpt-4o",
            "timestamp": datetime.now().isoformat()
        }
    )
