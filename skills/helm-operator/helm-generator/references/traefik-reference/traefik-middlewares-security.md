# Traefik Middlewares: Security & Access Control

This reference defines common Traefik Middlewares used for securing endpoints and throttling traffic. Use these patterns when the user requests authentication, IP whitelisting, or rate limiting.

## 1. BasicAuth
Restricts access to services using basic HTTP authentication.

**Common Use Case**: Protecting an internal dashboard or admin portal.

> **Important**: The `users` array contains `username:hashed-password` strings. Passwords must be hashed using MD5, SHA1, or BCrypt (e.g., using `htpasswd`). For Kubernetes, `secret` referencing is preferred in production, but `users` array works for simple cases.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    basic-auth:
      basicAuth:
        # Example: test / test
        users:
          - "test:$apr1$H6uskkkW$IgXLP6ewTrSuBkTrqE8wj/"
        # OR use a secret (preferred):
        # secret: my-auth-secret
        removeHeader: true # Removes the Authorization header before sending to backend
```

## 2. IPAllowList
Accepts or rejects requests based on the client's IP address.

**Common Use Case**: Restricting an endpoint to internal corporate IP ranges only.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    internal-only:
      ipAllowList:
        sourceRange:
          - "192.168.1.0/24"
          - "10.0.0.0/8"
```

## 3. RateLimit
Ensures that services receive a fair number of requests and prevents them from being overwhelmed.

**Common Use Case**: Preventing DDoS or brute-force attacks on login endpoints.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    rate-limit:
      rateLimit:
        # average requests per second per IP
        average: 100
        # maximum request burst
        burst: 50
```

## 4. Headers
Manages request and response headers, enabling HSTS, frame options, and custom header injection.

**Common Use Case**: Setting secure HTTP headers (CORS, HSTS, X-Frame-Options) exactly like an NGINX proxy might do.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    secure-headers:
      headers:
        ### CORS ###
        accessControlAllowMethods:
          - GET
          - OPTIONS
          - PUT
        accessControlAllowOriginList:
          - "https://example.com"
        accessControlMaxAge: 100
        
        ### Security ###
        frameDeny: true
        sslRedirect: true
        stsSeconds: 31536000
        stsIncludeSubdomains: true
        stsPreload: true
        contentTypeNosniff: true
        browserXssFilter: true
```
