import pytest
from fastapi import HTTPException
from backend.auth import Principal, require_scope

def test_cross_tenant_scope_is_rejected():
    with pytest.raises(HTTPException) as error:
        require_scope(Principal("u", "tenant-a", "branch-a"), "tenant-b", "branch-b")
    assert error.value.status_code == 403
