"""ASGI entrypoint. Uvicorn startup keeps the existing fail-closed behavior."""

from trustflow.api.factory import (
    ApprovalRequest as ApprovalRequest,
)
from trustflow.api.factory import (
    Container as Container,
)
from trustflow.api.factory import (
    CreateWorkflowRequest as CreateWorkflowRequest,
)
from trustflow.api.factory import (
    create_app as create_app,
)
from trustflow.api.factory import (
    get_container as get_container,
)

app = create_app()
