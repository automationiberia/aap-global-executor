# AAP Global Executor

Ansible Automation Platform (AAP) Configuration as Code to keep a **marker team**
of cross-organization executors in sync with the **Organization Execute** role on
every organization.

When a user is added to the marker team (typically via an authenticator map / LDAP
group), this project grants them Organization Execute on all orgs. When they leave
the marker team, those role assignments are revoked.

## Contents

| Path | Purpose |
|------|---------|
| `sync_marker_team_roles.yml` | Main sync playbook (team, map, role assign/revoke) |
| `bootstrap_aap_runner.yml` | Creates inventory, project, job template, and hourly schedule on AAP |
| `collections/requirements.yml` | Collections installed into the job / project |
| `vars/secrets.yml.example` | Template for vaulted API connection settings |

## How it works

```text
LDAP / IdP group ──► Authenticator map ──► Marker team membership
                                                    │
                                                    ▼
                              sync_marker_team_roles.yml (hourly)
                                                    │
                         ┌──────────────────────────┴──────────────────────────┐
                         ▼                                                     ▼
              Grant Organization Execute                         Revoke Organization Execute
              for each org (members)                             for users no longer in team
```

1. **Ensure** the marker team and authenticator map exist (`infra.aap_configuration.dispatch`).
2. **Export** organizations and role-user assignments from the live AAP instance with
   `infra.aap_configuration_extended.filetree_create`, then load them with
   `filetree_read`.
3. **Derive** marker-team members from assignments with role `Team Member` on the
   marker team object.
4. **Compute** desired `Organization Execute` assignments (member × organization)
   and revocations for users who left the team. Assignments already present in the
   exported state are skipped (delta only).
5. **Apply** the remaining payload with `gateway_role_user_assignments` (or skip
   dispatch entirely when there is nothing to change).

## Prerequisites

- AAP 2.5+ (Gateway API)
- An organization that will own the project (default: `Global Operations`)
- An Ansible Vault credential on AAP (to decrypt `vars/secrets.yml`)
- A Red Hat Ansible Automation Platform (API) credential on AAP (injects `CONTROLLER_*`)
- An execution environment that can reach the Gateway URL and includes (or can
  install) the collections listed in `collections/requirements.yml`
- Collections on the control node used for bootstrap:

  ```bash
  ansible-galaxy collection install -r collections/requirements.yml
  ```

## Secrets

Copy the example file, fill in values, encrypt with ansible-vault:

```bash
cp vars/secrets.yml.example vars/secrets.yml
# edit vars/secrets.yml
ansible-vault encrypt vars/secrets.yml --vault-password-file .vault_pass
```

| Variable | Description |
|----------|-------------|
| `vault_aap_hostname` | Gateway host (`https://aap.example.com` or `host:port` for a tunnel) |
| `vault_aap_username` | API username |
| `vault_aap_password` | API password |
| `vault_aap_validate_certs` | TLS verification (`true` / `false`) |

`vars/secrets.yml` and `.vault_pass` are gitignored and must never be committed.

When the job runs **on AAP**, the attached API credential’s `CONTROLLER_HOST`,
`CONTROLLER_USERNAME`, `CONTROLLER_PASSWORD`, and `CONTROLLER_VERIFY_SSL`
environment variables take precedence over vault values.

## Configurable sync inputs

Set these in `sync_marker_team_roles.yml` (or override with `-e` / extra vars):

| Variable | Default | Meaning |
|----------|---------|---------|
| `marker_team_name` | `Cross Executors` | Marker team name |
| `marker_team_org` | `Global Operations` | Organization that owns the team |
| `target_role` | `Organization Execute` | Role granted on every organization |
| `team_member_role` | `Team Member` | Role used to detect team membership |
| `marker_map_name` | `Cross Executors membership` | Authenticator map name |
| `marker_map_authenticator` | `AD LDAP` | Existing authenticator name on Gateway |
| `marker_ldap_group_cn` | `CN=cross-executors,CN=Users,DC=example,DC=com` | AD/LDAP group DN used by the authenticator map (`groups.has_or`) |

### Job Template extra var (recommended)

Keep the generic default in git, and set your real group on the Job Template
under **Variables** (or pass `-e` locally):

```yaml
marker_ldap_group_cn: "CN=crossexecutors,CN=Users,DC=example,DC=com"
```

Scheduled runs pick up Job Template variables automatically; no prompt-on-launch
is required.

## Bootstrap (create AAP runner objects)

Creates:

- Inventory + `localhost` (connection: local)
- Project pointing at this public Git repo
- Job template with Vault + API credentials and the chosen EE
- Hourly schedule

Edit the bootstrap vars first so names match **your** AAP objects:

```yaml
ge_vault_credential: "CHANGE_ME_VAULT_CREDENTIAL"       # existing Vault credential name
ge_aap_api_credential: "CHANGE_ME_AAP_API_CREDENTIAL"   # existing AAP/API credential name
ge_execution_environment: "CHANGE_ME_EE"                # existing EE name
project_scm_url: "https://github.com/EXAMPLE_ORG/aap-global-executor.git"
```

Then run:

```bash
ansible-playbook bootstrap_aap_runner.yml --vault-password-file .vault_pass
```

## Run the sync

### Locally (e.g. SSH tunnel to Gateway)

```bash
ansible-playbook sync_marker_team_roles.yml --vault-password-file .vault_pass
```

### On AAP

Launch the **Sync Marker Team Roles** job template (or wait for the hourly
schedule). The project is configured with `scm_update_on_launch: true`.

Required job template attachments:

1. Vault credential → decrypts `vars/secrets.yml`
2. AAP API credential → injects `CONTROLLER_*` for Gateway calls
3. Execution environment able to call the Gateway HTTPS endpoint
4. Project collection install enabled so `collections/requirements.yml` is
   applied to the job (required for a working `ansible.platform` with
   `filetree_create` / `gateway_api`)

## Collections

See `collections/requirements.yml`:

- `infra.aap_configuration` — dispatch / gateway object roles
- `infra.aap_configuration_extended` — `filetree_create` / `filetree_read` export
- `ansible.platform` (`>=2.7.20260812`) — token + `gateway_api` lookup used by
  `filetree_create` (older builds fail with a misleading `retryable` error)
- `ansible.controller` — controller objects used by bootstrap

Before `filetree_create`, the sync playbook applies a small in-place patch to the
loaded `ansible.platform` copy so base `PlatformError` always defines
`retryable` (upstream bug in the HTTP retry helper).

## Security notes

- Do not commit `vars/secrets.yml`, `.vault_pass`, or any file with live passwords.
- This repository must not contain real end-user names; membership is discovered
  at runtime from the AAP API.
- Prefer attaching platform credentials on the job template rather than embedding
  long-lived secrets in playbooks.
