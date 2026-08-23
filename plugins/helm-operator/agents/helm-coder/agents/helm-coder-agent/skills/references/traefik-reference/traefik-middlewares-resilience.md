# Traefik Middlewares: Resilience & Fault Tolerance

This reference defines common Traefik Middlewares used for maintaining service reliability. Use these patterns when the user requests circuit breakers, retry logic, or buffering.

## 1. CircuitBreaker
Prevents cascading failures by stopping requests to an unhealthy service once an error threshold is crossed. It fails fast when open, and attempts recovery progressively.

**Common Use Case**: Protecting overloaded backend services from receiving further traffic until they recover.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    backend-circuit-breaker:
      circuitBreaker:
        # Opens if 50% of requests return 5xx errors
        expression: "ResponseCodeRatio(500, 600, 0, 600) > 0.5"
        # OR latency-based:
        # expression: "LatencyAtQuantileMS(50.0) > 100"
```

## 2. Retry
Automatically re-issues the request if there is a network error (like a timeout) from the backend.

**Common Use Case**: Handling transient network failures and flaky connections gracefully.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    flaky-backend-retry:
      retry:
        attempts: 4 
        initialInterval: 100ms # Time between the first and second attempt
```

## 3. Buffering
Limits the size of requests that can be forwarded to services, reading them fully into memory/disk first. This protects backend services from taking long times to ingest large payloads.

**Common Use Case**: Handling large file uploads without tying up backend server connection threads (`multipart/form-data`).

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    upload-buffer:
      buffering:
        # Reject bodies larger than 2MB
        maxRequestBodyBytes: 2000000 
        # Buffer to disk if larger than 1MB
        memRequestBodyBytes: 1048576 
        # Protect against massive responses
        maxResponseBodyBytes: 10485760 
```

## 4. InFlightReq
Limits the number of simultaneous requests to the backend. Excess requests get an HTTP 429 Too Many Requests response.

**Common Use Case**: Protecting a fragile backend database from query storms during traffic spikes.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    limit-connections:
      inFlightReq:
        amount: 10
        sourceCriterion:
          # Limit per client IP
          ipStrategy:
            depth: 1 
```
