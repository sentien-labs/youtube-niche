# Security Policy

## Supported Versions

The public project is pre-1.0. Security fixes target the default branch until versioned releases
begin.

## Reporting A Vulnerability

Please do not open public issues for leaked keys, OAuth tokens, private cache databases, or any
bug that exposes private data.

Use GitHub private vulnerability reporting if it is enabled for the repository. If it is not
enabled yet, contact the repository owner and include:

- a short description of the issue;
- steps to reproduce it;
- whether API keys, OAuth tokens, cache contents, or generated reports are affected;
- suggested remediation, if known.

## Data Safety Notes

Do not commit `.env`, OAuth token files, raw cache databases, private reports, or outputs that
contain private channel/customer research. Use synthetic fixtures for examples and tests.
