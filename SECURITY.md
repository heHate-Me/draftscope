# Security policy

DraftScope is alpha research software. Security fixes are considered for the
current release line and the `main` branch; older releases are not maintained.
There is no guaranteed response or resolution time.

| Version | Security updates |
| --- | --- |
| 0.1.x | Current |
| Earlier versions | Not maintained |

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue, discussion,
dataset, or pull request.

Use GitHub's **Security** tab and select **Report a vulnerability** for
[`heHate-Me/draftscope`](https://github.com/heHate-Me/draftscope/security/advisories/new).
If private vulnerability reporting is unavailable, contact the repository
owner through the [heHate-Me GitHub profile](https://github.com/heHate-Me)
without publishing exploit details, and ask for a private reporting channel.

Include, when possible:

- the affected version or commit;
- the operating system and Python version;
- the security impact and affected component;
- minimal reproduction steps or a proof of concept; and
- any suggested mitigation.

Remove API keys, Keychain contents, private player data, local paths, and other
sensitive information before submitting a report. Never use production
credentials in a reproduction.

## Scope

Security reports can include credential exposure, unsafe redirect or download
handling, archive or path traversal, untrusted-file processing, integrity-check
bypasses, and unintended disclosure through generated artifacts. Prediction
quality, missing public statistics, data-source coverage, and ordinary model
disagreements are not security vulnerabilities; report those through GitHub
Issues.

The project has no bug-bounty program. Downloaded datasets and third-party
services remain governed by their own security policies, licenses, and terms.
