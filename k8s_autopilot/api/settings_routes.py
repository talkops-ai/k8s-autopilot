from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from typing import Optional
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("settings_routes")
import psycopg
import psycopg.rows
import os

async def test_slack_token(token: str) -> tuple[bool, str]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return True, ""
                return False, data.get("error", "Invalid Slack token")
            return False, f"Slack API returned HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

async def test_github_token(token: str) -> tuple[bool, str]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json"
                }
            )
            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 401:
                return False, "Bad credentials / unauthorized"
            return False, f"GitHub API returned HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

async def test_argocd_connection(url: str, token: str) -> tuple[bool, str]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            clean_url = url.rstrip("/")
            resp = await client.get(
                f"{clean_url}/api/v1/applications?limit=1",
                headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 200:
                return True, ""
            return False, f"ArgoCD API returned HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

async def fetch_and_validate_argocd_token(url: str, username: str, password: str, verify_ssl: bool = False) -> tuple[Optional[str], Optional[str]]:
    import httpx
    clean_url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=verify_ssl) as client:
            resp = await client.post(
                f"{clean_url}/api/v1/session",
                json={"username": username, "password": password}
            )
            if resp.status_code != 200:
                return None, f"Failed to authenticate with ArgoCD: HTTP {resp.status_code}"
            
            data = resp.json()
            token = data.get("token")
            if not token:
                return None, "No token returned in ArgoCD session response"
                
            val_resp = await client.get(
                f"{clean_url}/api/v1/applications?limit=1",
                headers={"Authorization": f"Bearer {token}"}
            )
            if val_resp.status_code != 200:
                return None, f"Token validation failed: HTTP {val_resp.status_code}"
                
            return token, None
    except Exception as e:
        return None, str(e)

def create_settings_routes(config) -> list[Route]:
    from k8s_autopilot.core.hitl.checkpointer import get_database_uri

    async def get_settings(request: Request) -> JSONResponse:
        db_uri = get_database_uri(config)
        if not db_uri:
            return JSONResponse({"detail": "Database not configured"}, status_code=500)
        try:
            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    await cur.execute("""
                        SELECT key, value, default_value, type, category, mcp_server_id, is_sensitive, is_editable, display_name, description, updated_by, updated_at 
                        FROM k8s_autopilot_settings 
                        ORDER BY category, key;
                    """)
                    rows = await cur.fetchall()
                    for r in rows:
                        r["updated_at"] = r["updated_at"].isoformat()
                    return JSONResponse(rows)
        except Exception as e:
            return JSONResponse({"detail": f"Database error: {e}"}, status_code=500)

    async def get_setting(request: Request) -> JSONResponse:
        key = request.path_params["key"]
        db_uri = get_database_uri(config)
        if not db_uri:
            return JSONResponse({"detail": "Database not configured"}, status_code=500)
        try:
            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    await cur.execute("""
                        SELECT key, value, default_value, type, category, mcp_server_id, is_sensitive, is_editable, display_name, description, updated_by, updated_at 
                        FROM k8s_autopilot_settings 
                        WHERE key = %s;
                    """, (key,))
                    row = await cur.fetchone()
                    if not row:
                        return JSONResponse({"detail": "Setting not found"}, status_code=404)
                    row["updated_at"] = row["updated_at"].isoformat()
                    return JSONResponse(row)
        except Exception as e:
            return JSONResponse({"detail": f"Database error: {e}"}, status_code=500)

    async def put_settings(request: Request) -> JSONResponse:
        data = await request.json()
        if not isinstance(data, list):
            return JSONResponse({"detail": "Invalid payload format, expected list of settings"}, status_code=400)
            
        db_uri = get_database_uri(config)
        if not db_uri:
            return JSONResponse({"detail": "Database not configured"}, status_code=500)
            
        try:
            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                async with conn.cursor() as cur:
                    for item in data:
                        key = item.get("key")
                        value = item.get("value")
                        mcp_server_id = item.get("mcp_server_id")
                        if not key or value is None:
                            continue
                            
                        if value == "******":
                            continue
                            
                        await cur.execute("SELECT type FROM k8s_autopilot_settings WHERE key = %s;", (key,))
                        row = await cur.fetchone()
                        if not row:
                            from k8s_autopilot.config.db_config import _determine_category, _is_sensitive
                            if mcp_server_id:
                                category_str = 'mcp'
                            else:
                                category_str = _determine_category(key)
                                if category_str not in ('llm', 'integration', 'mcp', 'system', 'langsmith'):
                                    category_str = 'mcp'
                            sensitive_bool = _is_sensitive(key)
                            from k8s_autopilot.config.db_config import SETTINGS_METADATA
                            meta = SETTINGS_METADATA.get(key, {})
                            display_name = meta.get("display_name", key.replace("_", " ").title())
                            description = meta.get("description", f"Custom configuration for {key}")
                            
                            await cur.execute("""
                                INSERT INTO k8s_autopilot_settings 
                                (key, value, default_value, type, category, mcp_server_id, is_sensitive, is_editable, display_name, description, updated_by)
                                VALUES (%s, %s, NULL, 'str', %s, %s, %s, TRUE, %s, %s, 'admin');
                            """, (key, str(value), category_str, mcp_server_id, sensitive_bool, display_name, description))
                            continue
                            
                        type_str = row[0]
                        try:
                            from k8s_autopilot.config.db_config import deserialize_value
                            deserialize_value(str(value), type_str)
                        except Exception as te:
                            return JSONResponse({"detail": f"Type validation failed for key {key}: {te}"}, status_code=400)
                            
                        if mcp_server_id:
                            await cur.execute("""
                                UPDATE k8s_autopilot_settings 
                                SET value = %s, mcp_server_id = %s, category = 'mcp', updated_by = 'admin', updated_at = CURRENT_TIMESTAMP 
                                WHERE key = %s;
                            """, (str(value), mcp_server_id, key))
                        else:
                            await cur.execute("""
                                UPDATE k8s_autopilot_settings 
                                SET value = %s, updated_by = 'admin', updated_at = CURRENT_TIMESTAMP 
                                WHERE key = %s;
                            """, (str(value), key))
            return JSONResponse({"success": True})
        except Exception as e:
            return JSONResponse({"detail": f"Database error: {e}"}, status_code=500)

    async def delete_setting(request: Request) -> JSONResponse:
        key = request.path_params["key"]
        db_uri = get_database_uri(config)
        if not db_uri:
            return JSONResponse({"detail": "Database not configured"}, status_code=500)
            
        try:
            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT default_value FROM k8s_autopilot_settings WHERE key = %s;", (key,))
                    row = await cur.fetchone()
                    if not row:
                        return JSONResponse({"detail": "Setting not found"}, status_code=404)
                    default_val = row[0]
                    
                    if default_val is not None:
                        await cur.execute("""
                            UPDATE k8s_autopilot_settings 
                            SET value = default_value, updated_by = 'system', updated_at = CURRENT_TIMESTAMP 
                            WHERE key = %s;
                        """, (key,))
                    else:
                        await cur.execute("""
                            DELETE FROM k8s_autopilot_settings 
                            WHERE key = %s;
                        """, (key,))
            return JSONResponse({"success": True})
        except Exception as e:
            return JSONResponse({"detail": f"Database error: {e}"}, status_code=500)

    async def get_db_setting_value(key: str) -> str | None:
        db_uri = get_database_uri(config)
        if not db_uri:
            return None
        try:
            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT value FROM k8s_autopilot_settings WHERE key = %s;", (key,))
                    row = await cur.fetchone()
                    return row[0] if row else None
        except Exception:
            return None

    async def test_integration(request: Request) -> JSONResponse:
        data = await request.json()
        integration_type = data.get("type")
        payload = data.get("payload", {})
        
        success = False
        error = ""
        new_token = None
        
        if integration_type == "slack":
            token = payload.get("SLACK_BOT_TOKEN")
            if token == "******":
                token = await get_db_setting_value("SLACK_BOT_TOKEN")
            if token:
                success, error = await test_slack_token(token)
            else:
                error = "Missing SLACK_BOT_TOKEN"
                
        elif integration_type == "github":
            token = payload.get("GITHUB_PERSONAL_ACCESS_TOKEN")
            if token == "******":
                token = await get_db_setting_value("GITHUB_PERSONAL_ACCESS_TOKEN")
            if token:
                success, error = await test_github_token(token)
            else:
                error = "Missing GITHUB_PERSONAL_ACCESS_TOKEN"
                
        elif integration_type == "argocd":
            url = payload.get("ARGOCD_SERVER_URL")
            username = payload.get("ARGOCD_USERNAME")
            password = payload.get("ARGOCD_PASSWORD")
            token = payload.get("ARGOCD_AUTH_TOKEN")
            insecure = payload.get("ARGOCD_INSECURE", True)
            
            if not url or url == "******":
                url = await get_db_setting_value("ARGOCD_SERVER_URL")
            if username == "******":
                username = await get_db_setting_value("ARGOCD_USERNAME")
            if password == "******":
                password = await get_db_setting_value("ARGOCD_PASSWORD")
            
            if isinstance(insecure, str):
                verify_ssl = insecure.lower() not in ("true", "1", "yes")
            elif isinstance(insecure, bool):
                verify_ssl = not insecure
            else:
                verify_ssl = False
                
            logger.info(f"[settings_routes] argocd test integration url={url} username={username} password={'***' if password else None} token={'***' if token else None} insecure={insecure}")
            if url and username and password:
                new_token, fetch_err = await fetch_and_validate_argocd_token(url, username, password, verify_ssl=verify_ssl)
                logger.info(f"[settings_routes] fetch_and_validate_argocd_token result success={bool(new_token)} err={fetch_err}")
                if new_token:
                    db_uri = get_database_uri(config)
                    if db_uri:
                        try:
                            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                                async with conn.cursor() as cur:
                                    await cur.execute(
                                        "UPDATE k8s_autopilot_settings SET value = %s, updated_by = 'system', updated_at = CURRENT_TIMESTAMP WHERE key = 'ARGOCD_AUTH_TOKEN';",
                                        (new_token,)
                                    )
                            success = True
                            error = ""
                        except Exception as e:
                            success = False
                            error = f"Successfully fetched token but failed to update database: {e}"
                    else:
                        config.set("ARGOCD_AUTH_TOKEN", new_token)
                        success = True
                        error = ""
                else:
                    success = False
                    error = fetch_err or "Unknown error fetching ArgoCD token"
            else:
                if token == "******" or not token:
                    token = await get_db_setting_value("ARGOCD_AUTH_TOKEN")
                if url and token:
                    success, error = await test_argocd_connection(url, token)
                else:
                    error = "Missing ARGOCD_SERVER_URL, ARGOCD_AUTH_TOKEN, or Username/Password"
        else:
            error = f"Unsupported integration type: {integration_type}"
            
        resp_data = {"success": success, "error": error}
        if integration_type == "argocd" and success and new_token:
            resp_data["token"] = new_token
        return JSONResponse(resp_data)

    async def get_settings_health(request: Request) -> JSONResponse:
        db_uri = get_database_uri(config)
        if not db_uri:
            return JSONResponse({"db_connected": False, "in_sync": False})
            
        try:
            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT count(*), max(updated_at) FROM k8s_autopilot_settings;")
                    row = await cur.fetchone()
                    db_count = row[0] if row else 0
                    db_max_updated = row[1].isoformat() if row and row[1] else None
                    
                    local_count = len(config._db_overrides)
                    in_sync = (local_count == db_count) or (local_count > 0 and db_count > 0)
                    return JSONResponse({
                        "status": "healthy",
                        "db_connected": True,
                        "in_sync": in_sync,
                        "db_count": db_count,
                        "local_count": local_count,
                        "db_last_update": db_max_updated
                    })
        except Exception as e:
            return JSONResponse({"status": "unhealthy", "db_connected": False, "error": str(e)}, status_code=500)

    return [
        Route("/api/settings", get_settings, methods=["GET"]),
        Route("/api/settings/health", get_settings_health, methods=["GET"]),
        Route("/api/settings/test-integration", test_integration, methods=["POST"]),
        Route("/api/settings/{key}", get_setting, methods=["GET"]),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/settings/{key}", delete_setting, methods=["DELETE"]),
    ]
