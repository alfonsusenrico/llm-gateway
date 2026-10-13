"""Typed Python client for LLM Gateway."""

__version__ = "0.2.0"

from llm_gateway_client.client import GatewayClient, RetryPolicy, SyncGatewayClient
from llm_gateway_client.errors import (
    AuthenticationError,
    BudgetExhaustedError,
    ExecutionUnknownError,
    GatewayError,
    GatewayUnavailableError,
    InvalidRequestError,
    RetryableError,
)
from llm_gateway_client.models import (
    Allocation,
    CallResult,
    CatalogueModel,
    Completed,
    Identity,
    RefusalDelta,
    Started,
    StreamEvent,
    TextDelta,
    Usage,
    image,
    json_schema,
    message,
    pdf,
    text,
)

__all__ = [
    "Allocation",
    "AuthenticationError",
    "BudgetExhaustedError",
    "CallResult",
    "CatalogueModel",
    "Completed",
    "ExecutionUnknownError",
    "GatewayClient",
    "GatewayError",
    "GatewayUnavailableError",
    "Identity",
    "InvalidRequestError",
    "RefusalDelta",
    "RetryPolicy",
    "RetryableError",
    "Started",
    "StreamEvent",
    "SyncGatewayClient",
    "TextDelta",
    "Usage",
    "image",
    "json_schema",
    "message",
    "pdf",
    "text",
]
