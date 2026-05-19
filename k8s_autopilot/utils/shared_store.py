"""
Shared cross-domain Store for coordinator collaboration.

All coordinators access the same :class:`InMemoryStore` instance under the
``("shared",)`` namespace. Each coordinator writes structured findings
(topology discoveries, RCA summaries, resource inventories) that other
coordinators can read on demand via the virtual ``/shared/`` filesystem path.

Architecture
~~~~~~~~~~~~
The shared store uses LangGraph's ``InMemoryStore`` as the backing store.
Each coordinator's ``CompositeBackend`` routes ``/shared/`` to a
``StoreBackend`` pointing at this store with a fixed ``"shared"`` namespace.

This means agents can:
- ``write_file("/shared/observability/triage-context.md", ...)``
- ``read_file("/shared/k8s/pod-status-checkout.md")``

Usage::

    from k8s_autopilot.utils.shared_store import get_shared_store

    # In coordinator.build_store():
    shared = get_shared_store()
    # Mount shared as a route in CompositeBackend

    # In coordinator.make_backend():
    routes["/shared/"] = StoreBackend(runtime, namespace=lambda _: "shared")

Note:
    This is ``InMemoryStore`` — data is lost on restart. For production
    durability, swap with ``PostgresStore`` or ``RedisStore`` backed by
    the same connection pool as the checkpointer.
"""

from __future__ import annotations

from langgraph.store.memory import InMemoryStore

_SHARED_STORE: InMemoryStore | None = None

SHARED_NAMESPACE = ("shared",)


def get_shared_store() -> InMemoryStore:
    """Return the singleton shared cross-domain store.

    Thread-safe by virtue of the GIL; for async contexts this is
    called once during ``build_store()`` before the event loop
    starts serving requests.

    Returns:
        The global ``InMemoryStore`` instance shared across all coordinators.
    """
    global _SHARED_STORE  # noqa: PLW0603
    if _SHARED_STORE is None:
        _SHARED_STORE = InMemoryStore()
    return _SHARED_STORE


def reset_shared_store() -> None:
    """Reset the shared store (for testing).

    Clears all data and forces re-creation on next ``get_shared_store()`` call.
    """
    global _SHARED_STORE  # noqa: PLW0603
    _SHARED_STORE = None
