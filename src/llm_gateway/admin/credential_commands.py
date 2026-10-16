"""Protected credential input shared by the CLI and interactive menu."""

import getpass
import json
import sys
import warnings

from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from llm_gateway.admin.project_commands import actor
from llm_gateway.projects.credentials import CredentialStore, validate_secret
from llm_gateway.projects.service import RegistryError
from llm_gateway.settings import Settings


def read_credential(*, stdin: bool = False) -> SecretStr:
    if stdin:
        raw = sys.stdin.read(16_386)
        raw = raw.removesuffix("\n").removesuffix("\r")
    else:
        if not sys.stdin.isatty():
            raise RegistryError("Credential input requires a terminal or explicit --stdin.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                raw = getpass.getpass("Provider credential: ")
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            raise RegistryError(
                "Protected credential input was cancelled or unavailable."
            ) from None
    return validate_secret(raw)


async def set_credential(
    pool: AsyncConnectionPool, settings: Settings, project_id: str, provider: str, secret: SecretStr
) -> str:
    revision = await CredentialStore(pool, settings.provider_credential_encryption_key).set(
        project_id, provider, secret, actor=actor()
    )
    return f"Credential registered for {project_id}/{provider}; revision {revision}."


async def remove_credential(
    pool: AsyncConnectionPool, settings: Settings, project_id: str, provider: str
) -> str:
    await CredentialStore(pool, settings.provider_credential_encryption_key).remove(
        project_id, provider, actor=actor()
    )
    return f"Credential removed for {project_id}/{provider}."


async def list_credentials(pool: AsyncConnectionPool, settings: Settings, project_id: str) -> str:
    metadata = await CredentialStore(pool, settings.provider_credential_encryption_key).metadata(
        project_id
    )
    return json.dumps(metadata, indent=2)
