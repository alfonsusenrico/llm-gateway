"""Typed Python client for LLM Gateway."""

__version__ = "0.1.0"

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
    Identity,
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
    "ExecutionUnknownError",
    "GatewayClient",
    "GatewayError",
    "GatewayUnavailableError",
    "Identity",
    "InvalidRequestError",
    "RetryPolicy",
    "RetryableError",
    "SyncGatewayClient",
    "Usage",
    "image",
    "json_schema",
    "message",
    "pdf",
    "text",
]
