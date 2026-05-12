"""Azure DevOps HTTP client with Microsoft Entra ID authentication and lifecycle management."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from azure.identity import (
    AzureCliCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
    InteractiveBrowserCredential,
    ManagedIdentityCredential,
)

logger = logging.getLogger(__name__)

API_VERSION = "7.1"
AZDO_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


@dataclass
class AppContext:
    """Application context holding shared auth state."""

    organization: str | None
    project: str | None
    credential: (
        AzureCliCredential
        | InteractiveBrowserCredential
        | ClientSecretCredential
        | ManagedIdentityCredential
        | DefaultAzureCredential
    )


def get_http_client(
    credential: (
        AzureCliCredential
        | InteractiveBrowserCredential
        | ClientSecretCredential
        | ManagedIdentityCredential
        | DefaultAzureCredential
    ),
    timeout: float = 30.0,
) -> httpx.Client:
    """Create an httpx.Client pre-configured with a Bearer token from the credential."""
    token = credential.get_token(AZDO_SCOPE).token
    return httpx.Client(
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=timeout,
    )


def resolve_org(app_ctx: AppContext, organization: str | None) -> str:
    """Resolve the effective organization, raising if none is available."""
    effective = organization or app_ctx.organization
    if not effective:
        raise ValueError(
            "No Azure DevOps organization provided. Supply 'organization' on the tool "
            "input, or set AZDO_ORGANIZATION as a default."
        )
    return effective.strip()


def resolve_project(app_ctx: AppContext, project: str | None) -> str:
    """Resolve the effective project, raising if none is available."""
    effective = project or app_ctx.project
    if not effective:
        raise ValueError(
            "No Azure DevOps project provided. Supply 'project' on the tool "
            "input, or set AZDO_PROJECT as a default."
        )
    return effective.strip()


def build_url(organization: str, project: str, path: str) -> str:
    """Build an Azure DevOps REST API URL."""
    return f"https://dev.azure.com/{organization}/{project}/_apis/{path}"


def build_params(**kwargs) -> dict:
    """Build a params dict with the API version, filtering out None values."""
    params = {"api-version": API_VERSION}
    params.update({k: v for k, v in kwargs.items() if v is not None})
    return params


def _build_credential(auth_type: str):
    """Instantiate an azure-identity credential based on AZDO_AUTH_TYPE."""
    if auth_type == "azure_cli":
        return AzureCliCredential()
    if auth_type == "interactive":
        return InteractiveBrowserCredential()
    if auth_type == "client_secret":
        tenant_id = os.environ.get("AZDO_TENANT_ID", "")
        client_id = os.environ.get("AZDO_CLIENT_ID", "")
        client_secret = os.environ.get("AZDO_CLIENT_SECRET", "")
        missing = [
            name
            for name, val in (
                ("AZDO_TENANT_ID", tenant_id),
                ("AZDO_CLIENT_ID", client_id),
                ("AZDO_CLIENT_SECRET", client_secret),
            )
            if not val
        ]
        if missing:
            raise ValueError(
                f"AZDO_AUTH_TYPE=client_secret requires: {', '.join(missing)}"
            )
        return ClientSecretCredential(tenant_id, client_id, client_secret)
    if auth_type == "managed_identity":
        return ManagedIdentityCredential()
    if auth_type == "default":
        return DefaultAzureCredential()
    raise ValueError(
        f"Unknown AZDO_AUTH_TYPE '{auth_type}'. "
        "Valid values: azure_cli, interactive, client_secret, managed_identity, default"
    )


@asynccontextmanager
async def devops_lifespan(server) -> AsyncIterator[AppContext]:
    """FastMCP lifespan that initializes shared Azure DevOps auth state.

    Reads configuration from environment variables:
    - AZDO_AUTH_TYPE: Credential type (default: default)
        default          — DefaultAzureCredential (tries all methods in order) [recommended]
        azure_cli        — Azure CLI credential (az login)
        interactive      — Interactive browser login
        client_secret    — Service principal with client secret
                           (requires AZDO_TENANT_ID, AZDO_CLIENT_ID, AZDO_CLIENT_SECRET)
        managed_identity — Managed identity (Azure-hosted workloads)
    - AZDO_TENANT_ID:     Entra ID tenant ID (required for client_secret)
    - AZDO_CLIENT_ID:     Service principal client ID (required for client_secret)
    - AZDO_CLIENT_SECRET: Service principal client secret (required for client_secret)
    - AZDO_ORGANIZATION:  Default organization name (optional; can be supplied per-tool)
    - AZDO_PROJECT:       Default project name (optional; can be supplied per-tool)

    Yields:
        AppContext containing the credential and optional defaults.
    """
    auth_type = os.environ.get("AZDO_AUTH_TYPE", "default").lower()
    organization = os.environ.get("AZDO_ORGANIZATION")
    project = os.environ.get("AZDO_PROJECT")

    credential = _build_credential(auth_type)
    logger.info("Azure DevOps auth type: %s", auth_type)

    if organization:
        logger.info("Default Azure DevOps organization: %s", organization)
    else:
        logger.info("No AZDO_ORGANIZATION set; tools must supply 'organization'")

    if project:
        logger.info("Default Azure DevOps project: %s", project)
    else:
        logger.info("No AZDO_PROJECT set; tools must supply 'project'")

    app_ctx = AppContext(organization=organization, project=project, credential=credential)
    logger.info("Azure DevOps MCP server initialized")

    yield app_ctx

    logger.info("Azure DevOps MCP server shutting down")
