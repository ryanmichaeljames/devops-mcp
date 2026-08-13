# Security Policy

## Supported versions

Fixes land on the latest release only. Please reproduce on the current version of `devops-mcp` before reporting.

| Version | Supported |
|---------|-----------|
| 1.6.x   | Yes       |
| < 1.6   | No        |

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/ryanmichaeljames/devops-mcp/security/advisories/new). Please do not open a public issue for a vulnerability.

Useful things to include:

- What an attacker can do, and what they need in order to do it
- The affected tool or module, and the version you reproduced on
- Steps to reproduce, with organization and project names replaced by placeholders

**Never include credentials, personal access tokens, client secrets, or tenant or client IDs in a report.**

This project is maintained by one person in their own time, so there is no response-time guarantee. Expect an acknowledgement within a week or so, and a fix released once the issue is confirmed. Credit in the advisory and the changelog is offered unless you would rather stay anonymous.

## Scope

This is an MCP server that runs locally, alongside your MCP client, and talks to Azure DevOps with your own Microsoft Entra ID credentials. Reports about the server's own behaviour are in scope — for example credential or token handling, the token cache, injection through tool inputs, attachment paths escaping `AZDO_ATTACHMENT_ROOT`, or server state leaking into tool responses.

Out of scope:

- Vulnerabilities in Azure DevOps itself. Report those to [MSRC](https://msrc.microsoft.com/report).
- Anything requiring an attacker who already controls the machine the server runs on, or its environment variables.
- Consequences of deliberately relaxing the safety flags — for example enabling `AZDO_ALLOW_WRITE` or `AZDO_ALLOW_DELETE`.
- Reports produced by a scanner with no demonstrated impact.

## Hardening

For any deployment that is not a single developer on their own machine:

- Leave `AZDO_ALLOW_WRITE` and `AZDO_ALLOW_DELETE` unset unless write access is genuinely needed; they are off by default.
- Prefer `default` or `azure_cli` authentication over `client_secret`, so no long-lived secret sits in the client configuration.
- Use `AZDO_TOKEN_CACHE_PROFILE` to keep tenants separated rather than sharing one cache across environments.
- Scope the identity the server authenticates as to the projects it actually needs.
