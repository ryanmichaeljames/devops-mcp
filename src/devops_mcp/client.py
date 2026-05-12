"""Azure DevOps HTTP client with PAT authentication and lifecycle management."""

import base64
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

API_VERSION = "7.1"


@dataclass
class AppContext:
    """Application context holding shared auth state."""

    organization: str | None
    project: str | None
    pat: str


def _build_auth_headers(pat: str) -> dict[str, str]:
    """Build Basic auth headers from a PAT."""
    token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_http_client(pat: str, timeout: float = 30.0) -> httpx.Client:
    """Create an httpx.Client pre-configured with PAT authentication."""
    return httpx.Client(headers=_build_auth_headers(pat), timeout=timeout)


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


@asynccontextmanager
async def devops_lifespan(server) -> AsyncIterator[AppContext]:
    """FastMCP lifespan that initializes shared Azure DevOps auth state.

    Reads configuration from environment variables:
    - AZDO_PAT: Personal Access Token (required for all tool calls)
    - AZDO_ORGANIZATION: Default organization name (optional; can be supplied per-tool)
    - AZDO_PROJECT: Default project name (optional; can be supplied per-tool)

    Yields:
        AppContext containing PAT and optional defaults.
    """
    pat = os.environ.get("AZDO_PAT", "")
    organization = os.environ.get("AZDO_ORGANIZATION")
    project = os.environ.get("AZDO_PROJECT")

    if not pat:
        logger.warning(
            "AZDO_PAT is not set. All Azure DevOps tool calls will fail authentication."
        )

    if organization:
        logger.info("Default Azure DevOps organization: %s", organization)
    else:
        logger.info("No AZDO_ORGANIZATION set; tools must supply 'organization'")

    if project:
        logger.info("Default Azure DevOps project: %s", project)
    else:
        logger.info("No AZDO_PROJECT set; tools must supply 'project'")

    app_ctx = AppContext(organization=organization, project=project, pat=pat)
    logger.info("Azure DevOps MCP server initialized")

    yield app_ctx

    logger.info("Azure DevOps MCP server shutting down")
