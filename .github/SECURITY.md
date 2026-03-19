# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email: security@montenegronyc.com

You should receive a response within 48 hours. We will work with you to understand and address the issue before any public disclosure.

## Supported Versions

Only the latest version on the `main` branch is actively supported with security updates.

## Security Features

- Agent sandboxing via restricted system user
- Author allowlist for issue processing
- Credential isolation between admin and agent
- Process limits and output buffer caps
- Sensitive env vars stripped from agent subprocesses
