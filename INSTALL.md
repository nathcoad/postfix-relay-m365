# Installing the Microsoft 365 relay with app-only OAuth

This guide takes you from nothing to a running Postfix relay that sends
through `smtp.office365.com:587` using an **application identity** (OAuth 2.0
client credentials) rather than a signed-in user. Because no user session or
refresh token is involved, multi-factor authentication policies on the mailbox
have no effect on the relay.

```text
   your apps / devices                 this container                      Microsoft
   ───────────────────      ┌──────────────────────────────────┐    ─────────────────────────
   SMTP, no auth  ───────►  │ Postfix ──► sasl-xoauth2 (XOAUTH2)│ ─► smtp.office365.com:587
   host:1025                │                 ▲                 │
                            │   /etc/tokens/sender.tokens.json  │
                            │                 ▲                 │
                            │   oauth-token-helper daemon  ─────┼─► login.microsoftonline.com
                            │   (client_credentials grant,      │    /<tenant>/oauth2/v2.0/token
                            │    renews 5 min before expiry)    │
                            └──────────────────────────────────┘
                              tenant ID + client ID (.env)
                              client secret (Docker secret file)
```

Contents

1. [Prerequisites](#1-prerequisites)
2. [Microsoft Entra: register the application](#2-microsoft-entra-register-the-application)
3. [Exchange Online: authorise the application for the mailbox](#3-exchange-online-authorise-the-application-for-the-mailbox)
4. [Docker host: deploy the relay](#4-docker-host-deploy-the-relay)
5. [Verify](#5-verify)
6. [Operations](#6-operations)
7. [Troubleshooting](#7-troubleshooting)
8. [Migrating from the delegated (user token) setup](#8-migrating-from-the-delegated-user-token-setup)
9. [Reference: environment variables](#9-reference-environment-variables)

---

## 1. Prerequisites

Microsoft 365 side

- A tenant with Exchange Online.
- An account that can register applications and grant admin consent
  (Global Administrator, or Application Administrator plus Exchange
  Administrator).
- A mailbox the relay will send as, for example `relay@example.com`.
  Microsoft documents SMTP AUTH client submission against **licensed**
  mailboxes; a shared mailbox without a licence is not a supported sender.
- PowerShell 7 (Windows, macOS or Linux) for the Exchange Online steps.

Docker host side

- Docker Engine with Compose v2 (`docker compose version` works).
- Outbound access to `login.microsoftonline.com:443` and
  `smtp.office365.com:587`.
- Access to the image: either build it locally from this repository or pull
  `encode/postfix-relay-m365` from Docker Hub (published by GitHub Actions on
  every push to `master` and on version tags).

You will collect four values as you go:

| Value | Where it comes from | Goes into |
|---|---|---|
| Directory (tenant) ID | App registration > Overview | `.env` as `OAUTH_TENANT_ID` |
| Application (client) ID | App registration > Overview | `.env` as `OAUTH_CLIENT_ID` |
| Client secret **value** | App registration > Certificates & secrets | `secrets/oauth_client_secret` |
| Enterprise application **Object ID** | Enterprise apps > your app > Overview | PowerShell only (step 3) |

---

## 2. Microsoft Entra: register the application

All of this is in the [Microsoft Entra admin center](https://entra.microsoft.com/).
The paths below follow the current left-hand navigation, where everything
sits under **Entra ID**. Older layouts show the same blades under
**Identity > Applications**; the blade names are unchanged.

### 2.1 Create the app registration

1. Browse to **Entra ID > App registrations** and select **New registration**.
2. Name: `postfix-relay` (any name; it appears in sign-in logs).
3. Supported account types: **Single tenant only - <your tenant>** (older
   portals word this "Accounts in this organizational directory only").
4. Redirect URI: leave empty. The client-credentials flow has no browser
   redirect.
5. Select **Register**.
6. On the app's **Overview** page copy the **Application (client) ID** and the
   **Directory (tenant) ID**. The app is listed under the **Owned
   applications** tab of App registrations from now on.

### 2.2 Grant the Exchange Online application permission

1. In the app, open **API permissions > Add a permission**.
2. Select the **APIs my organization uses** tab and search for
   **Office 365 Exchange Online**.
3. Select **Application permissions** (not Delegated).
4. Tick **SMTP.SendAsApp** and select **Add permissions**.
5. Select **Grant admin consent for `<your tenant>`** and confirm. The Status
   column must show a green "Granted for ...". If the button is disabled, your
   account lacks the role to consent.
6. Optional tidy-up: remove the default **Microsoft Graph > User.Read**
   delegated permission. The relay does not use it.

Do **not** enable "Allow public client flows" under **Authentication**. That
setting was only needed by the old device-code sign-in and is unnecessary for
an application identity.

### 2.3 Create the client secret

1. Open **Certificates & secrets > Client secrets > New client secret**.
2. Description: `postfix-relay`. Expiry: Microsoft caps it at 24 months and
   recommends under 12; pick what your rotation routine can keep up with.
   (Microsoft prefers certificate credentials over secrets for production;
   this relay currently supports client secrets only.)
3. Select **Add**, then copy the **Value** column immediately. It is shown
   once. The **Secret ID** column is not the secret.
4. Put the expiry date in a calendar. When the secret expires the relay
   starts logging `AADSTS7000222` and mail queues until you rotate it
   (see [Operations](#6-operations)).

### 2.4 Find the enterprise application Object ID

Registering an app also creates an *enterprise application* (service
principal) in the tenant. Exchange Online needs that object's ID.

1. Browse to **Entra ID > Enterprise apps > All applications**.
2. Search for `postfix-relay`. If it does not appear, set the **Application
   type** filter to **All applications** and select **Apply**.
3. On its **Overview** page copy the **Object ID**.

This is **not** the Object ID shown on the *App registration* overview page.
The two look alike; using the app registration's one produces
`535 5.7.3 Authentication unsuccessful` later with no further hint.

---

## 3. Exchange Online: authorise the application for the mailbox

Run these in PowerShell 7. Replace the placeholders.

```powershell
Install-Module -Name ExchangeOnlineManagement -Scope CurrentUser
Import-Module ExchangeOnlineManagement
Connect-ExchangeOnline -Organization "example.onmicrosoft.com"   # tenant ID also works

$AppId    = "<Application (client) ID from step 2.1>"
$ObjectId = "<Enterprise application Object ID from step 2.4>"
$Mailbox  = "relay@example.com"

# 3.1 Register the application's service principal in Exchange Online.
New-ServicePrincipal -AppId $AppId -ObjectId $ObjectId -DisplayName "postfix-relay"
$sp = Get-ServicePrincipal -Identity "postfix-relay"
$sp | Format-List DisplayName, AppId, ObjectId, Identity

# 3.2 Let the application act as the mailbox.
Add-MailboxPermission -Identity $Mailbox -User $sp.Identity -AccessRights FullAccess
Get-MailboxPermission -Identity $Mailbox | Where-Object { $_.User -like "*postfix-relay*" }

# 3.3 SMTP AUTH must be allowed for the mailbox (OAuth uses the same SMTP AUTH
#     command as basic auth; the organisation-wide switch and the per-mailbox
#     override both apply).
Get-TransportConfig | Format-List SmtpClientAuthenticationDisabled
Set-CASMailbox -Identity $Mailbox -SmtpClientAuthenticationDisabled $false
Get-CASMailbox -Identity $Mailbox | Format-List SmtpClientAuthenticationDisabled   # expect False

Disconnect-ExchangeOnline -Confirm:$false
```

Notes

- `New-ServicePrincipal` failing with a permissions error means the signed-in
  account is not an Exchange administrator.
- Permission changes can take up to an hour to propagate. If the SMTP test in
  step 5 fails with `535 5.7.3` right after these commands, wait and retry
  before changing anything.
- If **Security defaults** are enabled in Entra, SMTP AUTH is disabled
  tenant-wide and must be turned back on for this mailbox to work. Microsoft's
  reference is
  [Enable or disable SMTP AUTH in Exchange Online](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission).
- To send with other From addresses than `$Mailbox`, grant Send As on each:
  `Add-RecipientPermission -Identity other@example.com -Trustee $sp.Identity -AccessRights SendAs`.
  Without it Microsoft rejects the message with `5.7.60 SMTP; Client does not
  have permissions to send as this sender`.

---

## 4. Docker host: deploy the relay

### 4.1 Layout

```text
/opt/postfix-relay/                 git clone of this repository
├── docker-compose.yml              committed; reads the values below
├── .env                            NOT committed; tenant ID, client ID, sender, hostname
└── secrets/
    └── oauth_client_secret         NOT committed; the client secret value, mode 0600
```

`.env` and `secrets/` are listed in `.gitignore`. Never put the secret in
`.env`, in the compose file, or in the image.

### 4.2 Steps

```bash
sudo git clone <your git remote> /opt/postfix-relay
cd /opt/postfix-relay

# Deployment values
cp .env.example .env
$EDITOR .env          # OAUTH_TENANT_ID, OAUTH_CLIENT_ID, SENDER_ADDRESS, POSTFIX_MYHOSTNAME, SMTP_LISTEN, TZ

# Client secret as a file (no trailing newline needed; whitespace is stripped)
mkdir -p secrets
( umask 077 && printf '%s' '<client secret value from step 2.3>' > secrets/oauth_client_secret )
chmod 0600 secrets/oauth_client_secret

# Image: pull the published one, or build locally
docker compose pull
#   docker compose build      # build from this checkout instead

docker compose up -d
docker compose logs -f postfix
```

What `.env` holds

| Variable | Meaning |
|---|---|
| `OAUTH_TENANT_ID` | Directory (tenant) ID |
| `OAUTH_CLIENT_ID` | Application (client) ID |
| `SENDER_ADDRESS` | The mailbox from step 3. It becomes the XOAUTH2 user and should be the envelope sender your apps use. |
| `POSTFIX_MYHOSTNAME` | Name announced in EHLO to Microsoft (a hostname you control) |
| `SMTP_LISTEN` | Host side of the published port: `1025`, or `192.0.2.10:1025` to bind one address |
| `TZ` | Timezone for log timestamps |

The compose file refuses to start if any required value is missing, with a
message naming the variable.

### 4.3 What a good start looks like

```text
OAuth mode: client_credentials
oauth-token-helper: mode=client_credentials tenant=... client_id=... scope=https://outlook.office365.com/.default
oauth-token-helper: token endpoint: https://login.microsoftonline.com/.../oauth2/v2.0/token
oauth-token-helper: token file: /var/spool/postfix/etc/tokens/sender.tokens.json (renew 300s before expiry; secret from /run/secrets/oauth_client_secret)
oauth-token-helper: OAuth access token refreshed; expires in 3599 seconds (2026-09-02T05:41:12Z); next renewal in 3299 seconds
OAuth access token is in place
starting service Postfix-3.6.4-1ubuntu1.3 ...
```

Then `docker compose ps` shows the container as `healthy` within a minute.

---

## 5. Verify

### 5.1 Token acquisition (no mail is sent)

```bash
docker compose exec postfix oauth-token-test
```

```text
mode:           client_credentials
tenant:         ...
client_id:      ...
client_secret:  from /run/secrets/oauth_client_secret (40 chars, not shown)
scope:          https://outlook.office365.com/.default
token endpoint: https://login.microsoftonline.com/.../oauth2/v2.0/token
token file:     /var/spool/postfix/etc/tokens/sender.tokens.json (not written; use --write)
OK: access token obtained (HTTP 200, token_type=Bearer); expires in 3599 seconds (2026-09-02T05:41:12Z)
JWT claims (safe subset):
  aud:             https://outlook.office365.com
  iss:             https://sts.windows.net/<tenant>/
  appid:           <client id>
  app_displayname: postfix-relay
  tid:             <tenant>
  roles:           SMTP.SendAsApp
  ver:             1.0
  exp:             1788671472 (2026-09-02T05:41:12Z)
role check:     OK - SMTP.SendAsApp role present
```

Exit codes: 0 usable token, 1 acquisition failed (the Microsoft error is
printed), 2 token obtained but without the `SMTP.SendAsApp` role (go back to
step 2.2 and check admin consent). The access token itself is never printed.

### 5.2 Send a message through the relay

```bash
docker compose exec postfix smtp-send-test --from relay@example.com --to you@example.com --wait 60
```

```text
connecting to 127.0.0.1:25
accepted by relay: 2.0.0 Ok: queued as 4cXk3N2FzLzJ9Q
marker: relaytest-3f9a1c2b7d4e
OK: 4cXk3N2FzLzJ9Q has left the local queue (delivered to smtp.office365.com, or bounced).
confirm with:  docker compose logs postfix | grep 4cXk3N2FzLzJ9Q   (look for status=sent)
```

Then confirm the hand-off to Microsoft and check the recipient's inbox:

```bash
docker compose logs postfix | grep 4cXk3N2FzLzJ9Q
# ... relay=smtp.office365.com[...]:587, ... status=sent (250 2.0.0 OK ...)
```

The same test works from the host against the published port, for example
`bin/smtp-send-test --host 127.0.0.1 --port 1025 --from ... --to ...`, but
`--wait` only works inside the container.

### 5.3 Point your applications at the relay

Configure devices and applications to use the Docker host on the `SMTP_LISTEN`
port, no authentication, no TLS (the container trusts the network it is
exposed to, so bind `SMTP_LISTEN` to a private address). Use
`SENDER_ADDRESS` as the From address unless you granted Send As for others.

---

## 6. Operations

Token lifecycle

- The helper requests a token at container start and again 5 minutes before
  each expiry (`OAUTH_REFRESH_MARGIN`). Tokens last about an hour, so expect
  one request per hour, not one per message.
- Each new token is written to a temporary file and renamed into place, so
  Postfix never sees a partial file. The file is `postfix:postfix 0600`.
- If Microsoft cannot be reached, the helper logs the HTTP status and the
  `error` / `error_description` it received, keeps the still-valid token on
  disk, and retries with exponential backoff (5 s doubling to 5 min, honouring
  `Retry-After`). Mail is deferred in the Postfix queue, never lost.
- The container healthcheck fails when Postfix stops answering on port 25 or
  when no unexpired token is on disk: `docker compose ps` shows `unhealthy`.

Rotating the client secret

1. Create a new secret in Entra (step 2.3). Both secrets stay valid until the
   old one expires, so there is no outage window.
2. Replace the file: `( umask 077 && printf '%s' '<new value>' > secrets/oauth_client_secret )`.
3. `docker compose up -d --force-recreate postfix` (file-based secrets are
   read when the container is created).
4. `docker compose exec postfix oauth-token-test`, then delete the old secret
   in Entra.

Logs

- `docker compose logs -f postfix` shows Postfix, the helper and (if enabled)
  OpenDKIM. Look for `oauth-token-helper:` lines for token events.
- The helper never logs the token or the secret. On an SMTP authentication
  failure sasl-xoauth2 itself logs a summary line; if you enable
  `log_full_trace_on_failure` in `/etc/sasl-xoauth2.conf` the plugin's trace
  includes the access token, so leave it off in production.

Upgrading

```bash
cd /opt/postfix-relay && git pull
docker compose pull   # or: docker compose build
docker compose up -d
```

The queue lives in the named volume `postfix-spool` and survives
re-creation. `docker compose down -v` deletes it.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `configuration error: OAUTH_CLIENT_SECRET, OAUTH_CLIENT_SECRET_FILE or /run/secrets/oauth_client_secret is required` at start | Secret file missing or empty | Create `secrets/oauth_client_secret`; `docker compose up -d --force-recreate` |
| `HTTP 401 invalid_client: AADSTS7000215: Invalid client secret provided` | Wrong secret, or the Secret ID was copied instead of the Value | Create a new secret, copy the Value (step 2.3), rotate |
| `HTTP 401 invalid_client: AADSTS7000222: The provided client secret keys ... are expired` | Secret expired | Rotate (Operations) |
| `HTTP 400 unauthorized_client: AADSTS700016: Application ... was not found in the directory` | Client ID / tenant ID mismatch | Check both values against the app registration Overview |
| `HTTP 400 invalid_request: AADSTS90002: Tenant '...' not found` | Tenant ID mistyped | Copy the Directory (tenant) ID from the app registration Overview |
| `HTTP 400 invalid_scope` or role check `WARNING - token has no SMTP.SendAsApp role` | Permission not added or admin consent not granted | Step 2.2; wait a few minutes and rerun `oauth-token-test` |
| Postfix logs `SASL authentication failed ... 535 5.7.3 Authentication unsuccessful` while `oauth-token-test` is OK | Exchange side: wrong Object ID in `New-ServicePrincipal`, no `Add-MailboxPermission`, SMTP AUTH disabled, or propagation delay | Step 3 (re-check the *enterprise application* Object ID), wait up to an hour |
| `430 4.2.0 STOREDRV; mailbox logon failure ... MapiExceptionLogonFailed` (the hex diagnostic decodes to "AuthenticationContext has no rights on this session") | SMTP AUTH succeeded but Exchange cannot open the mailbox as the app: the `Add-MailboxPermission` grant has not propagated yet, or `New-ServicePrincipal` was given the wrong Object ID | Transient (4.x.x): Postfix keeps retrying. Wait up to an hour, then `postqueue -f`. If it persists, confirm the Object ID against Entra ID > Enterprise apps > your app > Overview |
| `5.7.60 SMTP; Client does not have permissions to send as this sender` | From address is not the authorised mailbox | Use `SENDER_ADDRESS`, or grant Send As (step 3 notes) |
| `WARNING: no OAuth access token after 30s; starting Postfix anyway` | Entra unreachable at boot | Check outbound 443; the helper keeps retrying, mail queues meanwhile |
| Container `unhealthy` | Postfix down, or token expired and cannot be renewed | `docker compose logs postfix`, then `docker compose exec postfix oauth-token-helper status` |
| Messages stuck in queue | Any of the above | `docker compose exec postfix postqueue -p` shows the deferral reason per message; `postqueue -f` retries now |

For a full picture of one message: `docker compose logs postfix | grep <queue id>`.

---

## 8. Migrating from the delegated (user token) setup

If this host previously ran the refresh-token flow (a `sender.tokens.json`
obtained with `sasl-xoauth2-tool get-token outlook --use-device-flow` and a
hand-copied `sasl-xoauth2.conf`):

1. Follow steps 2.2 to 2.4 and step 3 for the existing app registration. The
   old **Delegated > SMTP.Send** permission can be removed and **Allow public
   client flows** can be set back to No.
2. Create `.env` and `secrets/oauth_client_secret` as in step 4.
3. Delete the old artefacts: the local `sender.tokens.json` and
   `sasl-xoauth2.conf` copies, and any `docker cp` steps in your runbook. The
   entrypoint now generates `/etc/sasl-xoauth2.conf` and the token file itself.
4. `docker compose up -d --force-recreate`. Anything left in the old
   container's chroot token directory is overwritten on first start.

The legacy flow still works when no `OAUTH_*` variables are set (see the
README), but it is subject to the MFA policy that broke it.

---

## 9. Reference: environment variables

Read by the container. The compose file sets the ones marked "compose".

| Variable | Default | Notes |
|---|---|---|
| `OAUTH_GRANT_TYPE` | `client_credentials` if `OAUTH_TENANT_ID` is set, else `refresh_token` | compose sets `client_credentials` |
| `OAUTH_TENANT_ID` / `_FILE` | required | compose, from `.env` |
| `OAUTH_CLIENT_ID` / `_FILE` | required | compose, from `.env` |
| `OAUTH_CLIENT_SECRET` / `_FILE` | `/run/secrets/oauth_client_secret` if present | compose sets `_FILE`; prefer the file form |
| `OAUTH_SCOPE` | `https://outlook.office365.com/.default` | leave as is for Exchange Online |
| `OAUTH_TOKEN_ENDPOINT` | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` | override for sovereign clouds, for example `login.microsoftonline.us` |
| `OAUTH_TOKEN_FILE` | `/var/spool/postfix/etc/tokens/sender.tokens.json` | path on the container filesystem; `sasl_passwd` refers to it as `/etc/tokens/sender.tokens.json` because the Postfix `smtp` client is chrooted |
| `OAUTH_REFRESH_MARGIN` | `300` | seconds before expiry to renew |
| `OAUTH_HTTP_TIMEOUT` | `30` | seconds per token request |
| `OAUTH_STARTUP_TIMEOUT` | `30` | seconds the entrypoint waits for the first token before starting Postfix anyway |
| `OAUTH_TOKEN_FILE_OWNER` | `postfix:postfix` | owner of the token file |

Commands available inside the container

| Command | Purpose |
|---|---|
| `oauth-token-test [--write]` | Acquire one token, report expiry and safe claims; never prints the token |
| `oauth-token-helper status` | Exit 0 if an unexpired token is on disk (used by the healthcheck) |
| `oauth-token-helper validate` | Check the configuration without network access |
| `smtp-send-test --from A --to B [--wait N]` | Send one message through the local relay |
| `postqueue -p`, `postqueue -f` | Show / flush the Postfix queue |
