# Security policy

## Reporting a vulnerability

Please report security issues privately, not as a public issue.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository. If that is unavailable to you, open an issue saying only
that you have a report and how to reach you — no details — and a maintainer will
arrange a private channel.

We will acknowledge a report within a week and tell you what we intend to do
about it. Please give us a reasonable chance to release a fix before disclosing.

## What this platform holds

Worth knowing when assessing impact:

- **Credentials for other systems.** A GPU cluster's kubeconfig and an ssh
  identity for bare-metal machines. Both are how the platform launches anything,
  so a compromise of the platform is a compromise of that access.
- **Reversible encryption, keyed on `jwtSecret`.** Kubeconfigs are encrypted at
  rest rather than hashed, because they must be replayed to the cluster. The key
  is derived from the deployment's JWT secret, so that secret protects stored
  credentials as well as sign-in tokens.
- **Broad reach by design.** The worker launches containers on GPU machines and,
  on a shared box, stops and restarts services it finds there. Treat it as
  infrastructure with production adjacency, not as an ordinary web app.

## Scope

In scope: authentication and authorization, the launch path, secret handling,
injection into rendered commands and manifests, the policy session API.

Out of scope: anything requiring an attacker who already has cluster-admin or
root on a GPU machine; the security of the engines or the benchmark platform.
