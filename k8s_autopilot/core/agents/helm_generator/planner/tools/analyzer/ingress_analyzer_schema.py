from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional, Literal, Union
from enum import Enum

# ============================================================
# ENUMS
# ============================================================

class TraefikRouteType(str, Enum):
    """Type of Traefik routing"""
    HTTP = "HTTP"
    TCP = "TCP"
    UDP = "UDP"

class MatcherType(str, Enum):
    """Traefik matcher types (Rule)"""
    HOST = "Host"
    PATH_PREFIX = "PathPrefix"
    PATH = "Path"
    METHOD = "Method"
    HEADER = "Header"
    HEADER_REGEXP = "HeaderRegexp"
    QUERY = "Query"
    QUERY_REGEXP = "QueryRegexp"
    HOST_REGEXP = "HostRegexp"
    PATH_REGEXP = "PathRegexp"

class LoadBalancerStrategy(str, Enum):
    """TraefikService load balancing strategies"""
    WEIGHTED_ROUND_ROBIN = "weighted"
    HIGHEST_RANDOM_WEIGHT = "hrw"
    MIRRORING = "mirroring"

class MiddlewareType(str, Enum):
    """Traefik middleware types"""
    RATE_LIMIT = "RateLimit"
    BASIC_AUTH = "BasicAuth"
    DIGEST_AUTH = "DigestAuth"
    FORWARD_AUTH = "ForwardAuth"
    CORS = "Cors"
    HEADERS = "Headers"
    REDIRECT_SCHEME = "RedirectScheme"
    REDIRECT_PATH = "RedirectPath"
    REPLACE_PATH = "ReplacePath"
    REPLACE_PATH_REGEX = "ReplacePathRegex"
    STRIP_PREFIX = "StripPrefix"
    STRIP_PREFIX_REGEX = "StripPrefixRegex"
    COMPRESS = "Compress"
    CONTENT_TYPE = "ContentType"
    CIRCUIT_BREAKER = "CircuitBreaker"
    IN_FLIGHT_CONN = "InFlightConn"
    IP_WHITELIST = "IPWhitelist"
    IP_ALLOWLIST = "IPAllowList"
    IP_DENYLIST = "IPDenyList"
    REGEX_REDIRECT = "RegexRedirect"
    JSON_WEB_TOKEN = "JSONWebToken"

class TLSMinVersion(str, Enum):
    """TLS minimum versions"""
    TLS10 = "VersionTLS10"
    TLS11 = "VersionTLS11"
    TLS12 = "VersionTLS12"
    TLS13 = "VersionTLS13"

class CipherSuite(str, Enum):
    """TLS cipher suites"""
    TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256 = "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256"
    TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384 = "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384"
    TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256 = "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256"
    TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384 = "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384"
    TLS_RSA_WITH_AES_128_GCM_SHA256 = "TLS_RSA_WITH_AES_128_GCM_SHA256"
    TLS_RSA_WITH_AES_256_GCM_SHA384 = "TLS_RSA_WITH_AES_256_GCM_SHA384"

# ============================================================
# MATCHER CONFIGURATION
# ============================================================

class MatcherRule(BaseModel):
    """Single Traefik matcher rule"""
    matcher_type: MatcherType = Field(...)
    value: Union[str, List[str]] = Field(...)
    
    # Optional for specific matcher types
    host: Optional[str] = Field(None, description="For Host matcher")
    path: Optional[str] = Field(None, description="For Path matcher")
    method: Optional[str] = Field(None, description="For Method matcher")
    header_name: Optional[str] = Field(None, description="For Header/HeaderRegexp matcher")
    header_value: Optional[str] = Field(None, description="For Header/HeaderRegexp matcher")
    regexp_pattern: Optional[str] = Field(None, description="For regexp matchers")
    query_param: Optional[str] = Field(None, description="For Query matcher")
    
    @field_validator('value')
    @classmethod
    def validate_matcher_value(cls, v, values):
        """Validate matcher value format"""
        matcher_type = values.get('matcher_type')
        
        if matcher_type in [MatcherType.HOST, MatcherType.HOST_REGEXP]:
            if isinstance(v, str):
                if not re.match(r'^([a-z0-9]([-a-z0-9]*[a-z0-9])?\.)*[a-z0-9]([-a-z0-9]*[a-z0-9])?$', v):
                    raise ValueError(f"Invalid hostname: {v}")
        
        return v
    
    def to_traefik_rule(self) -> str:
        """Convert matcher to Traefik rule syntax"""
        if self.matcher_type == MatcherType.HOST:
            return f'Host(`{self.host}`)'
        elif self.matcher_type == MatcherType.PATH_PREFIX:
            return f'PathPrefix(`{self.path}`)'
        elif self.matcher_type == MatcherType.PATH:
            return f'Path(`{self.path}`)'
        elif self.matcher_type == MatcherType.METHOD:
            methods = self.value if isinstance(self.value, list) else [self.value]
            return f'Method(`{"`, `".join(methods)}`)'
        elif self.matcher_type == MatcherType.HEADER:
            return f'Header(`{self.header_name}`, `{self.header_value}`)'
        elif self.matcher_type == MatcherType.HEADER_REGEXP:
            return f'HeaderRegexp(`{self.header_name}`, `{self.regexp_pattern}`)'
        elif self.matcher_type == MatcherType.QUERY:
            return f'Query(`{self.query_param}`)'
        elif self.matcher_type == MatcherType.HOST_REGEXP:
            return f'HostRegexp(`{self.regexp_pattern}`)'
        elif self.matcher_type == MatcherType.PATH_REGEXP:
            return f'PathRegexp(`{self.regexp_pattern}`)'
        else:
            return str(self.value)
        
class TraefikRoute(BaseModel):
    """Single route configuration for IngressRoute"""
    matchers: List[MatcherRule] = Field(..., min_items=1)
    priority: Optional[int] = Field(None, ge=1, le=2147483647, description="Route priority for disambiguation")
    middlewares: List[Dict[str, str]] = Field(default=[], description="Middleware references")
    services: List[Dict[str, Any]] = Field(...)
    
    def get_combined_matcher(self) -> str:
        """Combine all matchers with AND operator"""
        if not self.matchers:
            raise ValueError("At least one matcher required")
        
        rules = [m.to_traefik_rule() for m in self.matchers]
        return " && ".join(rules)

# ============================================================
# MIDDLEWARE CONFIGURATION
# ============================================================

class RateLimitMiddleware(BaseModel):
    """Rate limiting middleware"""
    name: str
    average: int = Field(..., ge=1, description="Average requests per second")
    burst: int = Field(..., ge=1, description="Burst size")
    source_criterion: Literal["requesthost", "clientip"] = Field("clientip")

class BasicAuthMiddleware(BaseModel):
    """Basic authentication middleware"""
    name: str
    users: List[str] = Field(..., min_items=1, description="Users in htpasswd format")
    realm: Optional[str] = Field(None)
    header_field: Optional[str] = Field(None, description="Custom header field")
    remove_header: bool = Field(True)

class ForwardAuthMiddleware(BaseModel):
    """Forward authentication middleware"""
    name: str
    address: str = Field(..., description="Auth service URL")
    trust_forward_header: bool = Field(False)
    auth_response_headers: List[str] = Field(default=[])
    auth_request_headers: List[str] = Field(default=[])
    tls_insecure_skip_verify: bool = Field(False)
    tls_secret_name: Optional[str] = Field(None)

class CorsMiddleware(BaseModel):
    """CORS middleware"""
    name: str
    allowed_origins: List[str] = Field(default=["*"])
    allowed_methods: List[str] = Field(default=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    allowed_headers: List[str] = Field(default=["*"])
    expose_headers: List[str] = Field(default=[])
    max_age: int = Field(3600)
    allow_credentials: bool = Field(False)

class HeadersMiddleware(BaseModel):
    """Headers modification middleware"""
    name: str
    custom_request_headers: Dict[str, str] = Field(default={})
    custom_response_headers: Dict[str, str] = Field(default={})
    ssl_proxy_headers: Dict[str, str] = Field(default={})

class RedirectSchemeMiddleware(BaseModel):
    """Redirect scheme middleware"""
    name: str
    scheme: Literal["http", "https"]
    port: Optional[str] = Field(None, description="Override port (e.g., '8080')")
    permanent: bool = Field(False)

class StripPrefixMiddleware(BaseModel):
    """Strip prefix middleware"""
    name: str
    prefixes: List[str] = Field(..., min_items=1)
    force_slash: bool = Field(True)

class CompressMiddleware(BaseModel):
    """Compression middleware"""
    name: str
    min_response_body_bytes: int = Field(1024, ge=1)
    excluded_content_types: List[str] = Field(default=[])

class MiddlewareConfig(BaseModel):
    """Middleware configuration (polymorphic)"""
    type: MiddlewareType
    
    # Rate limiting
    rate_limit: Optional[RateLimitMiddleware] = None
    
    # Authentication
    basic_auth: Optional[BasicAuthMiddleware] = None
    forward_auth: Optional[ForwardAuthMiddleware] = None
    
    # CORS
    cors: Optional[CorsMiddleware] = None
    
    # Headers
    headers: Optional[HeadersMiddleware] = None
    
    # Redirects
    redirect_scheme: Optional[RedirectSchemeMiddleware] = None
    
    # Path manipulation
    strip_prefix: Optional[StripPrefixMiddleware] = None
    
    # Compression
    compress: Optional[CompressMiddleware] = None
    
    @field_validator('rate_limit')
    @classmethod
    def validate_rate_limit(cls, v, values):
        if values.get('type') == MiddlewareType.RATE_LIMIT and not v:
            raise ValueError("rate_limit config required for RateLimit middleware")
        return v

# ============================================================
# TRAEFIK SERVICE CONFIGURATION
# ============================================================

class TraefikServiceRef(BaseModel):
    """Reference to a TraefikService for advanced load balancing"""
    name: str = Field(..., description="TraefikService name")
    namespace: Optional[str] = Field(None)
    kind: Literal["TraefikService"] = "TraefikService"
    weight: int = Field(1, ge=1)

class WeightedService(BaseModel):
    """Service for weighted round robin"""
    name: str
    kind: Literal["Service", "TraefikService"] = "Service"
    namespace: Optional[str] = Field(None)
    port: Optional[int] = Field(None, ge=1, le=65535)
    weight: int = Field(1, ge=1, le=1000)
    sticky_cookie: Optional[str] = Field(None, description="Sticky cookie name for WRR")
    sticky_max_age: Optional[int] = Field(None, description="Cookie max age in seconds")

class MirrorService(BaseModel):
    """Service mirror configuration"""
    name: str
    kind: Literal["Service", "TraefikService"] = "Service"
    namespace: Optional[str] = Field(None)
    port: Optional[int] = Field(None, ge=1, le=65535)
    percent: int = Field(..., ge=0, le=100, description="Percentage of traffic to mirror")

class SimpleService(BaseModel):
    """Simple Kubernetes service backend"""
    name: str = Field(..., description="Kubernetes service name")
    namespace: Optional[str] = Field(None)
    port: int = Field(..., ge=1, le=65535)
    weight: int = Field(1, ge=1)
    native_lb: bool = Field(False, description="Use Kubernetes service clusterIP directly")
    node_port_lb: bool = Field(False, description="Use nodePort for external Traefik")

# ============================================================
# TLS CONFIGURATION
# ============================================================

class TLSOptions(BaseModel):
    """TLS configuration options"""
    min_version: TLSMinVersion = TLSMinVersion.TLS12
    cipher_suites: List[CipherSuite] = Field(default=[])
    prefer_server_cipher_suites: bool = Field(False)
    sni_strict: bool = Field(False)

class TLSConfig(BaseModel):
    """TLS certificate configuration"""
    enabled: bool = Field(True)
    secret_name: Optional[str] = Field(None, description="Kubernetes secret with tls.crt and tls.key")
    cert_resolver: Optional[str] = Field(None, description="Reference to cert resolver (ACME)")
    domains: List[Dict[str, Any]] = Field(default=[], description="Domains for ACME")
    passthrough: bool = Field(False, description="Delegate TLS to backend")
    tls_options_name: Optional[str] = Field(None, description="Reference to TLSOptions resource")
    tls_options_namespace: Optional[str] = Field(None)

# ============================================================
# MAIN INPUT SCHEMA
# ============================================================

class IngressRouteGenerationPlannerInput(BaseModel):
    """
    Input schema for IngressRoute/IngressRouteTCP/IngressRouteUDP generation.
    
    Assumes:
    - Traefik is already installed with CRDs
    - ingress.kubernetes.io/v1 API available
    - All middleware and service references are pre-existing
    """
    
    # ============================================================
    # REQUIRED FIELDS
    # ============================================================
    
    app_name: str = Field(
        ...,
        description="Application name",
        pattern=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
    )
    
    route_type: TraefikRouteType = Field(
        TraefikRouteType.HTTP,
        description="Type of route (HTTP, TCP, UDP)"
    )
    
    routes: List[TraefikRoute] = Field(
        ...,
        min_items=1,
        description="Routes with matchers, middlewares, and services"
    )
    
    # ============================================================
    # SERVICE CONFIGURATION
    # ============================================================
    
    service_kind: Literal["Simple", "TraefikService"] = Field(
        "Simple",
        description="Simple Kubernetes service or advanced TraefikService"
    )
    
    services: List[Union[SimpleService, TraefikServiceRef]] = Field(
        ...,
        min_items=1,
        description="Backend services"
    )
    
    # TraefikService advanced configuration
    traefik_service_name: Optional[str] = Field(
        None,
        description="Name of TraefikService to create for advanced LB"
    )
    
    load_balancer_strategy: Optional[LoadBalancerStrategy] = Field(
        None,
        description="TraefikService load balancing strategy (WRR, HRW, Mirroring)"
    )
    
    weighted_services: Optional[List[WeightedService]] = Field(
        None,
        description="Services for weighted round robin"
    )
    
    mirror_services: Optional[List[MirrorService]] = Field(
        None,
        description="Services for traffic mirroring"
    )
    
    main_service_for_mirror: Optional[Dict[str, Any]] = Field(
        None,
        description="Main service for mirroring"
    )
    
    # ============================================================
    # MIDDLEWARE CONFIGURATION
    # ============================================================
    
    middlewares: List[MiddlewareConfig] = Field(
        default=[],
        description="Middleware configurations to create"
    )
    
    middleware_refs: List[Dict[str, str]] = Field(
        default=[],
        description="References to existing middleware"
    )
    
    # ============================================================
    # TLS CONFIGURATION
    # ============================================================
    
    tls: TLSConfig = Field(
        default_factory=TLSConfig,
        description="TLS configuration"
    )
    
    # ============================================================
    # ENTRY POINTS
    # ============================================================
    
    entry_points: List[str] = Field(
        default=["websecure"] if "tls" else ["web"],
        description="Traefik entry points to bind to"
    )
    
    # ============================================================
    # METADATA
    # ============================================================
    
    labels: Dict[str, str] = Field(default={})
    annotations: Dict[str, str] = Field(default={})
    
    namespace: str = Field("default")
    
    # ============================================================
    # TRAEFIK-SPECIFIC OPTIONS
    # ============================================================
    
    route_priority: Optional[int] = Field(
        None,
        ge=1,
        le=2147483647,
        description="Global route priority"
    )
    
    strip_prefix: Optional[str] = Field(None, description="Prefix to strip from requests")
    auth_middleware: Optional[str] = Field(None, description="Authentication middleware name")
    rate_limit_middleware: Optional[str] = Field(None, description="Rate limiting middleware name")
    cors_enabled: bool = Field(False)
    
    # ============================================================
    # ADVANCED OPTIONS
    # ============================================================
    
    servers_transport_name: Optional[str] = Field(
        None,
        description="ServersTransport for custom backend connection"
    )
    
    pass_host_header: bool = Field(True, description="Forward Host header to backend")
    timeout: Optional[str] = Field(None, description="Request timeout (e.g., '30s')")
    
    # ============================================================
    # PLANNER SOURCE MAPPING
    # ============================================================
    
    planner_source_paths: Dict[str, str] = Field(default={})
    
    # ============================================================
    # VALIDATORS
    # ============================================================
    
    @field_validator('routes')
    @classmethod
    def validate_routes_have_services(cls, v):
        """Ensure all routes reference services"""
        for route in v:
            if not route.services:
                raise ValueError("Each route must have at least one service")
        return v
    
    @field_validator('load_balancer_strategy')
    def validate_traefik_service_strategy(cls, v, values):
        """Validate TraefikService configuration"""
        if v == LoadBalancerStrategy.WEIGHTED_ROUND_ROBIN:
            if not values.get('weighted_services'):
                raise ValueError("weighted_services required for WRR strategy")
        elif v == LoadBalancerStrategy.MIRRORING:
            if not values.get('mirror_services') or not values.get('main_service_for_mirror'):
                raise ValueError("mirror_services and main_service_for_mirror required for mirroring")
        return v
    
    @field_validator('entry_points')
    def validate_entry_points_format(cls, v):
        """Validate entry point names"""
        if not v:
            raise ValueError("At least one entry point required")
        for ep in v:
            if not re.match(r'^[a-z0-9]+$', ep):
                raise ValueError(f"Invalid entry point name: {ep}")
        return v
    
    class Config:
        use_enum_values = False
        extra = "forbid"
