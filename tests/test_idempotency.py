def test_idempotency_key_is_scoped_by_tenant():
    assert ("tenant-a", "request-1") != ("tenant-b", "request-1")
