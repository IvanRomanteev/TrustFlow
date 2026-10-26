import pytest

from trustflow.api.app import Container
from trustflow.core.settings import Settings


@pytest.fixture()
def container() -> Container:
    return Container(Settings(max_retries=2, step_timeout_seconds=5.0))
