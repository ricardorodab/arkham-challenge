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

The VM keeps credentials in root-owned files and supplies only the required values to
each service. Root and Docker administrators can read runtime secrets; restrict those
permissions accordingly. HTTPS and restricted containers reduce exposure but do not
replace application authorization, security updates, or disaster recovery.

See the [deployment guide](deploy/README.md) for the production trust boundary and
operational recovery procedures. Keep Azure account MFA enabled; GitHub's noninteractive
OIDC deployment identity does not require a stored Azure password or user MFA prompt.
