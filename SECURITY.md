# Security Policy

## Supported versions

Veloce is pre-1.0 (`0.x`). Security fixes land on the latest released
`0.x` version only; there is no back-port window before 1.0.

| Version | Supported |
|---------|-----------|
| latest `0.x` | ✅ |
| older `0.x` | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's **private vulnerability reporting**:
open the repository's **Security** tab and choose **"Report a
vulnerability"**. This creates a private advisory visible only to the
maintainers and to you.

A useful report includes:

- the affected component and version (or commit),
- a description of the issue and its impact,
- a minimal proof-of-concept or reproduction steps,
- any suggested remediation, if you have one.

## What to expect

- **Acknowledgement** within a few days of the report.
- An initial assessment (severity, affected versions) shortly after.
- A fix developed under the private advisory, with a coordinated
  release. We will keep you updated and credit you in the advisory and
  changelog unless you ask otherwise.
- Please allow a reasonable disclosure window before any public
  write-up so a fixed release can ship first.

## Scope

In scope: the `veloce` package — routing, the request/response pipeline,
dependency injection, middleware, the signing helpers, and the built-in
development server.

Out of scope: the built-in development server is **not** intended for
production (see `docs/guide/deployment.md`); deployment-hardening of a
production ASGI server (uvicorn, etc.) is that server's responsibility.

## Before 1.0

An external security review of the request path is planned ahead of the
1.0 release. Until then, treat the framework as pre-production software.
