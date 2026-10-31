# Security and publishing

This repository contains source code, tests and documentation only. Never publish
`.runtime/`, virtual environments, local environment files, credentials, private
keys, session recordings, checkpoint databases or target workspace copies.
Ignored files may still contain sensitive data; `.gitignore` does not sanitize an
archive of the working directory. Use `git archive` on a reviewed commit for
source exports and rebuild distribution packages from that commit.

The UI is intended for local trusted use. Agents can execute commands and access
the selected workspace using their local credentials. Review permission settings
before running against sensitive projects. Do not expose the UI to untrusted
networks.

For suspected vulnerabilities or accidental credential exposure, contact the
maintainer privately through the contact options on the GitHub profile. Do not
include secrets or private run artifacts in public issues. Revoke exposed
credentials before sharing a sanitized reproduction.
