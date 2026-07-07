import pytest
import uuid
from k8s_autopilot.core.integration.thread_store import ThreadStore

@pytest.mark.unit
def test_thread_store_deterministic_resolution():
    store = ThreadStore()
    
    platform = "slack"
    thread_ts = "1720234567.000100"
    
    # Resolve first time
    lg_id_1 = store.resolve(platform, thread_ts)
    
    # Resolve second time
    lg_id_2 = store.resolve(platform, thread_ts)
    
    # Must be deterministic and equal
    assert lg_id_1 == lg_id_2
    
    # Must be a valid UUID format
    val = uuid.UUID(lg_id_1)
    assert str(val) == lg_id_1
    
    # Expected UUIDv5 using DNS namespace and key
    expected_key = f"{platform}:{thread_ts}"
    expected_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, expected_key))
    assert lg_id_1 == expected_uuid

@pytest.mark.unit
def test_thread_store_cross_restart_stability():
    store1 = ThreadStore()
    store2 = ThreadStore()
    
    platform = "slack"
    thread_ts = "1720234567.000100"
    
    # Resolve on first instance (pre-restart simulation)
    lg_id_1 = store1.resolve(platform, thread_ts)
    
    # Resolve on second instance (post-restart simulation)
    lg_id_2 = store2.resolve(platform, thread_ts)
    
    # Must yield the same deterministic ID
    assert lg_id_1 == lg_id_2

@pytest.mark.unit
def test_thread_store_different_threads():
    store = ThreadStore()
    
    platform = "slack"
    ts_1 = "1720234567.000100"
    ts_2 = "1720234568.000200"
    
    lg_id_1 = store.resolve(platform, ts_1)
    lg_id_2 = store.resolve(platform, ts_2)
    
    assert lg_id_1 != lg_id_2

@pytest.mark.unit
def test_thread_store_reverse_lookup():
    store = ThreadStore()
    
    platform = "slack"
    thread_ts = "1720234567.000100"
    
    lg_id = store.resolve(platform, thread_ts)
    
    lookup = store.reverse_lookup(lg_id)
    assert lookup is not None
    assert lookup["platform"] == platform
    assert lookup["thread_id"] == thread_ts
    
    # Unknown ID should return None
    assert store.reverse_lookup(str(uuid.uuid4())) is None

@pytest.mark.unit
def test_thread_store_has_thread():
    store = ThreadStore()
    
    platform = "slack"
    thread_ts = "1720234567.000100"
    
    assert not store.has_thread(platform, thread_ts)
    
    store.resolve(platform, thread_ts)
    
    assert store.has_thread(platform, thread_ts)
