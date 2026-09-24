import os
import uuid

import httpx
import pytest

BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as http:
        yield http


@pytest.fixture()
def device_id() -> str:
    # Unique device per test keeps the shared database hermetic.
    return f"dev-{uuid.uuid4().hex[:16]}"
