# Security Policy

Ancroo Crew starts the runs of its employees through the Docker socket of the server. Whoever takes over the service takes over the server. What protects against what is described in [docs/security.md](docs/security.md).

## Supported versions

Fixes go to the latest version on `main` only. There are no release branches.

## Reporting a vulnerability

Please report vulnerabilities through [GitHub's private vulnerability reporting](https://github.com/ancroo/ancroo-crew/security/advisories/new), not as a public issue.

Particularly welcome are ways in which an employee, or an instruction from a text an employee reads, gets past the limits listed under "What is blocked technically": write access outside of what the task names, reading the Claude token or the access tokens of the services, reaching the UI or other services on the server, or getting data out of the company.

You can expect a first answer within a few days. A confirmed vulnerability is fixed as soon as possible.
