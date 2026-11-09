# Security policy

Report suspected vulnerabilities through this repository's **Security → Report a
vulnerability** page. Do not put credentials, account data, or exploit details in a
public issue. Rotate any exposed credential even after removing the published copy.

## Changes and deployment

The protected `main` branch requires a pull request, passing checks, resolved review
conversations, and linear history. Administrators follow the same rules. Force pushes
and branch deletion are blocked. `CODEOWNERS` routes changes to deployment, workflow,
build, and dependency files to the maintainer.

This is currently a single-maintainer repository: pull requests require CI, with zero
independent approvals. This is not an independent review guarantee. Add another trusted
reviewer before enabling required code-owner approval; authors cannot approve their
own pull requests. Keep maintainer accounts protected with two-factor authentication
or passkeys and review collaborator access regularly.

Merging deployable code or workflow changes is a privileged operation. The main-branch
GitHub OIDC identity can run root commands on the one deployment VM. Review changes to
`.github/`, `deploy/`, dependency manifests, and build files with that trust boundary in
mind. Test success cannot make a malicious authorized workflow safe.

## Public artifacts and private state

Source and container images are public. Runtime credentials, operator configuration,
databases, and analytical data must stay out of Git, image layers, build arguments,
and workflow logs. Docker's context privacy checks exercise excluded paths using
synthetic canaries. They complement secret scanning rather than prove arbitrary source
files contain no secrets.

Production credentials are stored in Azure Key Vault. A VM runtime identity retrieves
them over the private endpoint into role-specific files on host tmpfs. Containers receive
read-only secret files and path settings, with secret-value environment variables disabled;
their access to VM identity endpoints stays blocked. Root and Docker administrators can
read runtime secrets; restrict those
permissions accordingly. HTTPS and restricted containers reduce exposure but do not
replace application authorization, security updates, or disaster recovery.

See the [deployment guide](deploy/README.md) for the production trust boundary and
operational recovery procedures. Keep Azure account MFA enabled; GitHub's noninteractive
OIDC deployment identity does not require a stored Azure password or user MFA prompt.

## Automated checks and dependency updates

Pull requests, pushes and a weekly schedule run the `Security checks` gate. It fails
when a dependency audit, secret scan, or production-container check fails, is cancelled,
or is skipped. PR checks have read-only repository permissions; publishing permissions
exist only in the main-branch promotion job. Promotion transfers the exact accepted
image through a one-day artifact and verifies its image ID and source/revision labels.

PyPA pip-audit checks the exact runtime, development and build requirements. npm audits
the lockfile without running dependency scripts. Gitleaks scans Git history with full
redaction; it does not publish secret-bearing reports or PR comments. Dependabot opens
weekly reviewable updates for Python, npm, Docker, Compose and Actions; it does not
automatically merge them. `requirements-dev.txt` is the canonical development pin list;
`requirements-dev.lock` remains a compatible entry point that includes it.

Trivy reports all HIGH and CRITICAL image findings, including vendor status and findings
without an available fix. Findings with available fixes block promotion. Unfixed,
deferred and vendor `will_not_fix` findings remain visible for maintainer review; there
is no blanket CVE allowlist. An unavailable vendor fix does not establish that a finding
is harmless. Review residual Debian advisories and reduce the exposed functionality or
change the base when necessary. The runtime uses patched, digest-pinned Python and Node
images on the same Debian ABI, and removes pip after installation and dependency checks.

The initial 2026-11-08 Bookworm base audit reported residual CRITICAL
[CVE-2025-7458](https://security-tracker.debian.org/tracker/CVE-2025-7458) for SQLite
(`affected`, no available fix) and
[CVE-2023-45853](https://security-tracker.debian.org/tracker/CVE-2023-45853) for zlib
(`will_not_fix`). It also reported unfixed/deferred HIGH findings in util-linux,
ncurses, SQLite, zlib, gzip, libacl, systemd and Perl. These are scanner/vendor statuses
for review, and the current CI report remains the source for updated findings.
