import httpx
import pytest

from trustflow.api.app import Container
from trustflow.core.settings import Settings


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch):
    """Every test uses a dummy environment key, never a developer credential.

    Missing-key tests explicitly delete this value. Transport tests must use
    MockTransport; this fixture does not authorize network inference.
    """
    monkeypatch.setenv("MODEL_API_KEY", "offline-test-placeholder")
    real_send = httpx.HTTPTransport.handle_request

    def offline_send(transport, request):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError("Offline tests cannot send external HTTP requests")
        return real_send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline_send)


@pytest.fixture()
def container() -> Container:
    return Container(Settings(max_retries=2, step_timeout_seconds=5.0))
