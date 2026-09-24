# AAP Global Executor

Ansible Automation Platform (AAP) Configuration as Code that keeps
**Organization Execute** in sync across every organization for a fixed list of
executor usernames, after validating each account in Active Directory.

Eligible users (on the list, OK in AD, **and already present in AAP**) receive
Organization Execute on all orgs. Listed users that are missing, disabled,
locked, or password-expired in AD have that role revoked. Listed users that
have never logged into AAP are skipped (no create/revoke). Users outside the
list are left untouched.

## Contents

| Path | Purpose |
|------|---------|
| `sync_marker_team_roles.yml` | Main sync playbook (AD check + role assign/revoke) |
| `bootstrap_aap_runner.yml` | Credential type/credential, inventory, project, JT, schedule |
| `tasks/evaluate_ad_user.yml` | Per-user LDAP lookup + AD flag evaluation |
| `tasks/probe_aap_user.yml` | Per-user Gateway presence check (skip if never logged in) |
| `files/eval_ad_account.py` | AD account status helper (disabled / password / expiry) |
| `collections/requirements.yml` | Collections installed into the job / project |
| `ee/` | Minimal Execution Environment definition (`ansible-builder`) |
| `vars/secrets.yml.example` | Template for vaulted AAP + LDAP settings |

## How it works

```text
GE_EXECUTOR_USERS (env) ──► candidate usernames
        │
        ▼
   LDAP/AD lookup (GE_LDAP_* credential)
        │
        ├── not present in AAP Gateway users
        │         └── skip (no role create/revoke until first login)
        │
        ├── OK in AD + present in AAP
        │         └── grant Organization Execute on all orgs (delta)
        │
        └── NOT OK in AD + present in AAP
                  └── revoke Organization Execute for that user (delta)
```

1. Read the executor list from `GE_EXECUTOR_USERS` (or `ge_executor_users`).
2. For each username, query AD with `files/eval_ad_account.py` (`python-ldap`,
   or `ldapsearch` fallback)
   and evaluate account flags.
3. Export organizations and role-user assignments with
   `infra.aap_configuration_extended.filetree_create` / `filetree_read`.
4. Probe Gateway `/api/gateway/v1/users/` for each listed username; drop
   candidates that do not exist in AAP yet (no login → no Gateway user).
5. Compute a **delta** of creates and revokes vs the exported state.
6. Apply with `gateway_role_user_assignments` (skipped when empty).

Authenticator maps and marker teams are **not** used.

## Prerequisites

- AAP 2.5+ (Gateway API)
- An organization that will own the project (default: `Global Operations`)
- Ansible Vault credential (decrypt `vars/secrets.yml`)
- Red Hat Ansible Automation Platform API credential (`CONTROLLER_*`)
- Custom **LDAP Active Directory** credential (created by bootstrap; injects
  `GE_LDAP_*`)
- Execution environment that can reach Gateway **and** LDAP/AD. Use the
  definition under `ee/` (adds `python-ldap` + `openldap-clients`), or any EE
  that includes those packages
- Project collection install enabled for `collections/requirements.yml`

## Environment variables

| Variable | Source | Purpose |
|----------|--------|---------|
| `GE_EXECUTOR_USERS` | JT / runtime env (or extra var `ge_executor_users`) | Comma-separated usernames to manage |
| `GE_LDAP_SERVER` | LDAP credential injector | LDAP URI (`ldaps://dc.example.com:636`) |
| `GE_LDAP_BIND_DN` | LDAP credential injector | Bind DN |
| `GE_LDAP_BIND_PASSWORD` | LDAP credential injector | Bind password |
| `GE_LDAP_SEARCH_BASE` | LDAP credential injector | Search base (`DC=example,DC=com`) |
| `GE_LDAP_VALIDATE_CERTS` | LDAP credential injector | `true` / `false` |
| `CONTROLLER_*` | AAP API credential | Gateway API connection |

### Job Template configuration

1. Attach Vault, AAP API, and **Global Executor LDAP** credentials.
2. Set the user list, either:
   - Job Template **Variables**:

     ```yaml
     ge_executor_users: "user1,user2,user3"
     ```

   - or an environment variable `GE_EXECUTOR_USERS=user1,user2,user3` if your
     execution environment / instance group injects it (env wins when non-empty).

## AD eligibility rules

A listed user is **eligible** when the LDAP search finds the account and:

- `userAccountControl` does **not** have `ACCOUNTDISABLE` (0x2)
- password is not marked expired (`PASSWORD_EXPIRED` / `pwdLastSet == 0`)
- `accountExpires` is never or still in the future
- account is not locked (`lockoutTime == 0`)

Otherwise the user is **ineligible** and any managed Organization Execute
assignments for that username are revoked.

## Secrets

```bash
cp vars/secrets.yml.example vars/secrets.yml
# edit vars/secrets.yml
ansible-vault encrypt vars/secrets.yml --vault-password-file .vault_pass
```

| Variable | Description |
|----------|-------------|
| `vault_aap_*` | Gateway API connection (local / bootstrap) |
| `vault_ldap_*` | LDAP bind used locally and to seed the AAP LDAP credential |

`vars/secrets.yml` and `.vault_pass` are gitignored.

## LDAP credential type

AAP has **no** built-in credential type suitable for playbook LDAP binds. Bootstrap
creates:

- Credential type: `LDAP Active Directory` (injects `GE_LDAP_*`)
- Credential: `Global Executor LDAP` (when `vault_ldap_*` are set in secrets)

If vault LDAP vars are omitted, the type is still created; create the credential
in the UI and attach it to the Job Template.

## Bootstrap

Edit placeholders in `bootstrap_aap_runner.yml`, then:

```bash
ansible-playbook bootstrap_aap_runner.yml --vault-password-file .vault_pass
```

## Run the sync

### Locally

```bash
export GE_EXECUTOR_USERS="user1,user2"
ansible-playbook sync_marker_team_roles.yml --vault-password-file .vault_pass
```

### On AAP

Launch **Sync Marker Team Roles** (or wait for the hourly schedule).

## Execution environment

`ee/` defines a **minimal** image on
`ansible-automation-platform-26/ee-minimal-rhel9` with only what AD validation
needs beyond the base image:

| Layer | Content |
|-------|---------|
| Python | `python-ldap` (`ee/requirements.txt`) |
| System | `openldap-clients` (+ compile deps for `python-ldap`) |
| Collections | *not* baked in — use project `collections/requirements.yml` |

Build and publish:

```bash
# registry.redhat.io login required for the base image
podman login registry.redhat.io

ansible-builder build \
  -f ee/execution-environment.yml \
  -t global-executor-ee:latest \
  -v3

podman tag global-executor-ee:latest YOUR_REGISTRY/global-executor-ee:latest
podman push YOUR_REGISTRY/global-executor-ee:latest
```

Register the image in AAP as an Execution Environment, point the Job Template
(or `ge_execution_environment` in secrets) at it, then re-run the sync.

## Collections

See `collections/requirements.yml`:

- `infra.aap_configuration` / `infra.aap_configuration_extended`
- `ansible.platform` (`>=2.7.20260812`) for `filetree_create` / `gateway_api`
- `ansible.controller` for bootstrap objects

AD lookups use `files/eval_ad_account.py` with `python-ldap` when available,
otherwise `ldapsearch` from OpenLDAP clients (no extra Ansible collection).
The EE must include at least one of those.

Before `filetree_create`, the sync playbook patches `PlatformError.retryable` in
the loaded `ansible.platform` copy (upstream HTTP-retry helper bug).

## Security notes

- Do not commit `vars/secrets.yml`, `.vault_pass`, or live passwords.
- Prefer JT credentials for LDAP/API secrets; keep executor usernames in JT
  variables or env, not in git.
- Scope is limited to usernames listed in `GE_EXECUTOR_USERS` /
  `ge_executor_users`.
