# Traefik Middlewares: Routing & Path Manipulation

This reference defines common Traefik Middlewares used for routing manipulation. Use these patterns when the user requests path prefixes, redirects, or URI rewriting.

## 1. StripPrefix
Removes the specified prefixes from the URL path before forwarding the request to the backend.

**Common Use Case**: Hosting an API on `/api` but the backend expects requests at `/`.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    stripprefix:
      stripPrefix:
        prefixes:
          - /api
          - /v1
```

## 2. AddPrefix
Updates the path of a request by adding a prefix before forwarding it.

**Common Use Case**: The backend expects all requests to be prefixed with `/app`, but the external route is `/`.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    addprefix:
      addPrefix:
        prefix: /app
```

## 3. ReplacePath / ReplacePathRegex
Replaces the path of the request before forwarding. `replacePathRegex` is more powerful as it uses regular expressions.

**Common Use Case**: Migrating from an old path structure to a new one internally.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    replacepath:
      replacePath:
        path: /new-path
    replacepathregex:
      replacePathRegex:
        regex: ^/foo/(.*)
        replacement: /bar/$1
```

## 4. RedirectScheme / RedirectRegex
Redirects requests to a different scheme (e.g., HTTP to HTTPS) or to a completely different URL using regex.

**Common Use Case**: Forcing HTTPS, or redirecting deprecated endpoints.

```yaml
# values.yaml example
ingress:
  customMiddlewares:
    redirect-https:
      redirectScheme:
        scheme: https
        permanent: true
    redirect-api:
      redirectRegex:
        regex: ^http://(.*)/old-api/(.*)
        replacement: https://$1/new-api/$2
        permanent: true
```

## Integrating with IngressRoute

To attach these middlewares to an `IngressRoute`, reference them in the `hosts` block. The name MUST include the chart's fullname prefix since the template automatically prepends it:

```yaml
# values.yaml example
ingress:
  hosts:
    - host: api.example.com
      path: /api
      middlewares:
        # Note: the prefix is defined in the ingress-pattern.md template
        - name: "{{ include \"my-chart.fullname\" . }}-stripprefix"
```
