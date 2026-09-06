import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from k8s_autopilot.api.models import ThreadCreate, ThreadSearch, ThreadUpdate
from k8s_autopilot.api.service import get_thread_service


def create_thread_routes() -> list[Route]:
    """Factory that returns Starlette routes for the Thread API."""

    async def create_thread(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)
            
        data = await request.json()
        req = ThreadCreate(**data)
        # TODO: Get user_id from auth context, using "default" for now
        resp = await service.create_thread(req, user_id="default")
        return JSONResponse(resp.model_dump(mode="json"))

    async def search_threads(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)
            
        data = await request.json()
        req = ThreadSearch(**data)
        # TODO: Enforce user_id from auth context
        threads = await service.search_threads(req)
        return JSONResponse({"items": [t.model_dump(mode="json") for t in threads]})

    async def get_thread(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)
            
        thread_id_str = request.path_params["thread_id"]
        resp = await service.get_thread(thread_id_str)
        if not resp:
            return JSONResponse({"detail": "Thread not found"}, status_code=404)
        return JSONResponse(resp.model_dump(mode="json"))

    async def update_thread(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)
            
        thread_id_str = request.path_params["thread_id"]
        data = await request.json()
        req = ThreadUpdate(**data)
        resp = await service.update_thread(thread_id_str, req)
        if not resp:
            return JSONResponse({"detail": "Thread not found"}, status_code=404)
        return JSONResponse(resp.model_dump(mode="json"))

    async def delete_thread(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)
            
        thread_id_str = request.path_params["thread_id"]
        deleted = await service.delete_thread(thread_id_str)
        if not deleted:
            return JSONResponse({"detail": "Thread not found"}, status_code=404)
        return JSONResponse({"detail": "Thread deleted"})

    async def get_thread_state(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)
            
        thread_id_str = request.path_params["thread_id"]
        resp = await service.get_thread_state(thread_id_str)
        if not resp:
            return JSONResponse({"detail": "Thread not found"}, status_code=404)
        return JSONResponse(resp.model_dump(mode="json"))

    async def get_thread_history(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)
            
        thread_id_str = request.path_params["thread_id"]
        limit = int(request.query_params.get("limit", 10))

        resp = await service.get_thread_history(thread_id_str, limit)
        if not resp:
            return JSONResponse({"detail": "Thread not found"}, status_code=404)
        return JSONResponse(resp.model_dump(mode="json"))

    async def get_thread_telemetry(request: Request) -> JSONResponse:
        service = get_thread_service()
        if not service:
            return JSONResponse({"detail": "Thread API not ready"}, status_code=503)

        thread_id_str = request.path_params["thread_id"]
        resp = await service.get_thread_telemetry(thread_id_str)
        if not resp:
            return JSONResponse({"detail": "Thread not found"}, status_code=404)
        return JSONResponse(resp.model_dump(mode="json"))

    return [
        Route("/threads", create_thread, methods=["POST"]),
        Route("/threads/search", search_threads, methods=["POST"]),
        Route("/threads/{thread_id}", get_thread, methods=["GET"]),
        Route("/threads/{thread_id}", update_thread, methods=["PATCH"]),
        Route("/threads/{thread_id}", delete_thread, methods=["DELETE"]),
        Route("/threads/{thread_id}/state", get_thread_state, methods=["GET"]),
        Route("/threads/{thread_id}/history", get_thread_history, methods=["GET"]),
        Route("/threads/{thread_id}/telemetry", get_thread_telemetry, methods=["GET"]),
    ]
