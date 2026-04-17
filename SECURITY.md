# Security Policy

## Reporting a Vulnerability

If you believe you have found a security vulnerability in CodePilot, please report it privately by email to `jahanzebahmed.mail@gmail.com`.

Please do not open a public GitHub issue for unpatched vulnerabilities.

When reporting, include as much of the following as possible:

- affected version
- operating system and environment details
- reproduction steps
- proof of concept or example prompt/input
- impact assessment
- any suggested mitigation

I will do my best to acknowledge reports promptly and provide status updates as investigation progresses.

## Supported Versions

Security support is currently provided on a best-effort basis for the latest released version on PyPI.

## Security Model

CodePilot is an embeddable autonomous agent runtime for software engineering tasks. It is designed to let application developers integrate autonomous execution into their own systems.

CodePilot intentionally prioritizes execution expressiveness, composability, and library-level embeddability. It is not designed to guarantee prevention of every destructive or unsafe action by a capable model.

Users and integrators are responsible for deployment-time controls appropriate to their environment, including:

- workspace boundaries
- process isolation
- credential scoping
- permission gating
- audit logging
- infrastructure policy and runtime supervision

## Non-Goals

The following are not guaranteed by the core runtime:

- preventing all malicious or unsafe model behavior
- defending against arbitrary prompt injection in every deployment context
- acting as a complete sandbox or operating system security boundary
- replacing infrastructure-level policy enforcement

## Recommended Deployment Practices

If you deploy CodePilot in production or semi-trusted environments, consider:

- running inside containers or isolated workers
- restricting filesystem scope
- restricting shell access where possible
- using short-lived scoped credentials
- enabling explicit permission checks for sensitive tools
- logging tool invocations and file mutations
- reviewing prompts and tool surfaces exposed to the model

## Disclosure

Once a report is confirmed and a fix or mitigation is available, the issue can be disclosed publicly in a coordinated manner.
