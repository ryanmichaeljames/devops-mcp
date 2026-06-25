"""Unit tests for client.build_url percent-encoding.

Covers:
- Project name with a space is encoded as %20 in the URL.
- Organization name with a space is encoded as %20 in the URL.
- A multi-segment path keeps its '/' separators intact (not encoded).
- Path segments with special characters are encoded individually.
- Normal identifiers (no special chars) pass through unchanged.
"""

import pytest

from devops_mcp.client import build_url

# ---------------------------------------------------------------------------
# Encoding correctness
# ---------------------------------------------------------------------------


def test_project_with_space_is_percent_encoded():
    """A project name containing a space must appear as %20 in the URL."""
    url = build_url("myorg", "My Project", "wit/workitems")
    assert "My%20Project" in url
    assert "My Project" not in url


def test_organization_with_space_is_percent_encoded():
    """An organization name containing a space must appear as %20 in the URL."""
    url = build_url("My Org", "myproject", "wit/workitems")
    assert "My%20Org" in url
    assert "My Org" not in url


def test_project_with_special_chars_is_encoded():
    """A project name with '&' is encoded; '/' in path is preserved."""
    url = build_url("myorg", "R&D", "wit/workitems")
    assert "R%26D" in url
    assert "R&D" not in url


# ---------------------------------------------------------------------------
# Path separators are preserved
# ---------------------------------------------------------------------------


def test_path_slashes_are_preserved():
    """'/' in the path argument must survive encoding as literal slashes."""
    url = build_url("myorg", "myproject", "git/repositories/abc123/pullrequests/42")
    # The path should appear verbatim after /_apis/ — slashes intact.
    assert "/_apis/git/repositories/abc123/pullrequests/42" in url


def test_path_slashes_not_double_encoded():
    """Path slashes must not be percent-encoded to %2F."""
    url = build_url("myorg", "myproject", "build/builds/99/logs/1")
    assert "%2F" not in url.split("/_apis/", 1)[1]


# ---------------------------------------------------------------------------
# Normal (no special chars) inputs pass through unchanged
# ---------------------------------------------------------------------------


def test_plain_inputs_unchanged():
    """Alphanumeric org/project/path must appear verbatim in the URL."""
    url = build_url("contoso", "MyProject", "pipelines")
    assert url == "https://dev.azure.com/contoso/MyProject/_apis/pipelines"


# ---------------------------------------------------------------------------
# URL structure
# ---------------------------------------------------------------------------


def test_url_structure():
    """Built URL must start with the Azure DevOps base and contain /_apis/."""
    url = build_url("org", "proj", "some/path")
    assert url.startswith("https://dev.azure.com/")
    assert "/_apis/" in url
