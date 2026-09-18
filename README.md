# postfix-relay-m365

[![CI](https://github.com/nathcoad/postfix-relay-m365/actions/workflows/ci.yml/badge.svg)](https://github.com/nathcoad/postfix-relay-m365/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/docker/v/encode/postfix-relay-m365?label=docker%20hub&sort=semver)](https://hub.docker.com/r/encode/postfix-relay-m365)

Postfix SMTP relay for Microsoft 365 using **app-only (client credentials)
OAuth 2.0** with sasl-xoauth2. No user mailbox login, no MFA breakage.

```
docker pull encode/postfix-relay-m365
```

## Overview

This is a fork of [phesster/postfix-relay-xoauth2](https://github.com/phesster/postfix-relay-xoauth2),
which incorporates [tarickb's SASL-XOAuth2](https://github.com/tarickb/sasl-xoauth2/)
into [mwader's Postfix-Relay](https://hub.docker.com/r/mwader/postfix-relay/),
rebased on an [Ubuntu](https://hub.docker.com/_/ubuntu) 24.04 image and
extended with app-only OAuth for Microsoft 365. For detailed information on
any of these, please read their specific documentation.

The relay accepts unauthenticated SMTP from your network and submits to
`smtp.office365.com:587` with XOAUTH2. Two ways of getting the OAuth access
token are supported:

| Mode | `OAUTH_GRANT_TYPE` | How the token is obtained | Affected by mailbox MFA |
|---|---|---|---|
| **App-only** (recommended) | `client_credentials` | `oauth-token-helper` inside the container performs the client-credentials grant with the tenant ID, client ID and client secret, and rewrites the token file before each expiry. No user, no refresh token. | No |
| Delegated (legacy) | `refresh_token` | You obtain a user refresh token once (device flow / Gmail flow) and mount it; sasl-xoauth2 refreshes it itself. | Yes: Entra MFA enforcement breaks the refresh with `AADSTS50076` |

When `OAUTH_GRANT_TYPE` is unset the container picks `client_credentials` if
`OAUTH_TENANT_ID` is set and `refresh_token` otherwise, so existing
deployments keep working unchanged.

## Microsoft 365 app-only setup

**[INSTALL.md](INSTALL.md)** is the step-by-step guide: Entra app
registration, the `SMTP.SendAsApp` permission and admin consent, registering
the service principal in Exchange Online, mailbox permission, SMTP AUTH,
the Docker host layout, verification and troubleshooting.

Short version, once the Microsoft side is done:

```bash
cp .env.example .env            # tenant ID, client ID, sender address, hostname
mkdir -p secrets && ( umask 077 && printf '%s' '<client secret>' > secrets/oauth_client_secret )
docker compose up -d --build
docker compose exec postfix oauth-token-test
docker compose exec postfix smtp-send-test --from relay@example.com --to you@example.com --wait 60
```

[docker-compose.yml](docker-compose.yml) is the deployment file; it reads the
identifiers from `.env` and the secret from `./secrets/oauth_client_secret`
(a file-backed Docker secret mounted at `/run/secrets/oauth_client_secret`).
None of those are committed.

### How it works inside the container

- [bin/oauth-token-helper](bin/oauth-token-helper) (Python 3, standard library
  only) runs as a daemon started by the entrypoint. It POSTs
  `grant_type=client_credentials`, `scope=https://outlook.office365.com/.default`
  to `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, and
  writes the result to the token file sasl-xoauth2 reads, atomically (temp
  file, fsync, rename) as `postfix:postfix 0600`. It renews
  `OAUTH_REFRESH_MARGIN` (300 s) before expiry, so about once an hour, and
  never per message.
- On failure it logs the HTTP status and Microsoft's `error` /
  `error_description` (never the secret or a token), keeps a still-valid token
  in place, and retries with exponential backoff capped at 5 minutes,
  honouring `Retry-After`.
- Postfix, `sasl_passwd` and `transport` are configured exactly as before.
  Each SMTP session to Microsoft re-reads the token file, so a renewed token is
  picked up without reloading Postfix.
- The entrypoint waits up to `OAUTH_STARTUP_TIMEOUT` (30 s) for the first
  token, then starts Postfix regardless; mail queues until a token exists.
- The healthcheck requires Postfix to answer on port 25 and, in this mode, an
  unexpired token on disk (`oauth-token-helper status`).

### Tools

| Command (inside the container) | Purpose |
|---|---|
| `oauth-token-test` | Acquire a token once; prints expiry and the safe JWT claims `aud`, `iss`, `appid`, `app_displayname`, `tid`, `roles`, `ver`, `exp`. Never prints the token. Exit 2 if `SMTP.SendAsApp` is missing. |
| `oauth-token-helper status` | Exit 0 if an unexpired token is on disk. |
| `oauth-token-helper validate` | Configuration check, no network. |
| `smtp-send-test --from A --to B [--wait N]` | Sends one message through the local relay and, with `--wait`, watches it leave the queue. |

### sasl-xoauth2 limitations that shaped this

Verified against sasl-xoauth2 0.27, the version in the noble PPA
(`src/token_store.cc`):

- The only grant type it can perform itself is `refresh_token`; there is no
  client-credentials support, hence the external helper.
- It refuses a token file without a `refresh_token` key, so the helper writes
  `"refresh_token": ""`.
- It refreshes on its own only when `now + refresh_window (10 s) >= expiry`.
  With the helper keeping `expiry` ahead of the clock that path is never
  taken; if the helper were dead and the token expired, the fallback refresh
  would fail cleanly (empty refresh token) and Postfix would defer.
- `/etc/sasl-xoauth2.conf` must contain `client_id` and `client_secret`. In
  app-only mode the entrypoint generates it with the real client ID and a
  placeholder secret, because on an auth failure the plugin logs its full
  refresh request, secret included, to syslog.
- Its failure trace (`log_full_trace_on_failure`) also contains the access
  token. It is off by default; leave it off in production.

### Security notes

- No credentials are baked into the image. IDs come from `.env`, the secret
  from a Docker secret file; `.gitignore` excludes both plus any
  `*.tokens.json`.
- Token file: `postfix:postfix 0600` inside the Postfix chroot; directory
  `0750`.
- TLS certificate verification is never disabled, for the token endpoint (system
  CA bundle) or for SMTP (`smtp_tls_security_level=encrypt` with the CA file).
- The helper refuses a non-`https` token endpoint except on loopback (used by
  the unit tests).

## Building and testing

```bash
# unit tests for the helper (mock token endpoint on loopback; no Entra needed)
python3 -m unittest discover -s tests -v

# lint the same way CI does
shellcheck -S error run
hadolint -t error Dockerfile

# build locally (or skip and `docker compose pull` the published image)
docker compose build

# run and verify
docker compose up -d
docker compose logs -f postfix
docker compose exec postfix oauth-token-test
docker compose exec postfix smtp-send-test --from relay@example.com --to you@example.com --wait 60
docker compose ps        # healthy
```

### Continuous integration and releases

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs the unit tests,
shellcheck, hadolint, a `docker compose config` render and an image build on
every push and pull request. Pushes to `master` and `v*` tags publish a
multi-arch (`linux/amd64`, `linux/arm64`) image to Docker Hub as
[encode/postfix-relay-m365](https://hub.docker.com/r/encode/postfix-relay-m365):

| Event | Tags |
|---|---|
| push to `master` | `latest`, `sha-<short commit>` |
| tag `vX.Y.Z` | `X.Y.Z`, `X.Y` |

Publishing needs the repository secrets `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN` (a Docker Hub access token with read/write scope).

## postfix-relay

Postfix SMTP relay docker image. Useful for sending email without using an
external SMTP server.

Default configuration is an open relay that relies on docker networking for
protection. Be careful to not expose it publicly.


### Postfix variables (postfix-relay)

Postfix [configuration options](http://www.postfix.org/postconf.5.html) can be set
using `POSTFIX_<name>` environment variables. See [Dockerfile](Dockerfile) for default
configuration. You probably want to set `POSTFIX_myhostname` (the FQDN used by 220/HELO).

Note that `POSTFIX_myhostname` will change the postfix option
[myhostname](http://www.postfix.org/postconf.5.html#myhostname).

You can modify master.cf using postconf with `POSTFIXMASTER_` variables. All double `__` symbols will be replaced with `/`. For example

### Postfix master.cf variables

```
environment:
...
- POSTFIXMASTER_submission__inet=submission inet n - y - - smtpd -o syslog_name=postfix/submission
...
```
will emit the following into the container and run that command

```
postconf -Me submission/inet="submission inet n - y - - smtpd -o syslog_name=postfix/submission"
```

### Postfix lookup tables

You can also create multiline [tables](http://www.postfix.org/DATABASE_README.html#types) using `POSTMAP_<filename>` like this example:
```
environment:
...
  - POSTFIX_transport_maps=hash:/etc/postfix/transport
  - |
    POSTMAP_transport=gmail.com smtp
    mydomain.com relay:[relay1.mydomain.com]:587
    * relay:[relay2.mydomain.com]:587
...
```
which will generate the file `/etc/postfix/transport` in the container
```
gmail.com smtp
mydomain.com relay:[relay1.mydomain.com]:587
* relay:[relay2.mydomain.com]:587
```
and run the command `postmap /etc/postfix/transport`.

### OpenDKIM variables

OpenDKIM [configuration options](http://opendkim.org/opendkim.conf.5.html) can be set
using `OPENDKIM_<name>` environment variables. See [Dockerfile](Dockerfile) for default
configuration. For example `OPENDKIM_Canonicalization=relaxed/simple`.

### Using docker run
```
docker run -e POSTFIX_myhostname=smtp.domain.tld encode/postfix-relay-m365
```

### Using docker-compose

See [docker-compose.yml](docker-compose.yml) for the Microsoft 365 app-only
deployment. A minimal generic example:

```
app:
  # use hostname "smtp" as SMTP server

smtp:
  image: encode/postfix-relay-m365
  restart: always
  environment:
    - POSTFIX_myhostname=smtp.domain.tld
    - OPENDKIM_DOMAINS=smtp.domain.tld
```

### Logging
By default container only logs to stdout. If you also wish to log `mail.*` messages to file on persistent volume, you can do something like:

```
environment:
  ...
  - RSYSLOG_LOG_TO_FILE=yes
  - RSYSLOG_TIMESTAMP=yes
volumes:
  - /your_local_path:/var/log/
```

You can also forward log output to remote syslog server if you define `RSYSLOG_REMOTE_HOST` variable. It always uses UDP protocol and port `514` as default value,
port number can be changed to different one with `RSYSLOG_REMOTE_PORT`. Default format of forwarded messages is defined by Rsyslog template `RSYSLOG_ForwardFormat`,
you can change it to [another template](https://www.rsyslog.com/doc/v8-stable/configuration/templates.html) (section Reserved Template Names) if you wish with `RSYSLOG_REMOTE_TEMPLATE` variable.

```
environment:
  ...
  - RSYSLOG_REMOTE_HOST=my.remote-syslog-server.com
  - RSYSLOG_REMOTE_PORT=514
  - RSYSLOG_REMOTE_TEMPLATE=RSYSLOG_ForwardFormat
```

#### Advanced logging configuration

If configuration via environment variables is not flexible enough it's possible to configure rsyslog directly: `.conf` files in the `/etc/rsyslog.d` directory will be [sorted alphabetically](https://www.rsyslog.com/doc/v8-stable/rainerscript/include.html#file) and included into the primary configuration.

### Timezone
Wrong timestamps in log can be fixed by setting proper timezone.
This parameter is handled by Ubuntu base image.

```
environment:
  ...
  - TZ=Europe/Prague
```

### Known issues

#### I see `key data is not secure: /etc/opendkim/keys can be read or written by other users` error messages.

Some Docker distributions like Docker for Windows and RancherOS seems to handle
volume permission in way that does not work with OpenDKIM default behavior of
ensuring safe permissions on private keys.

A workaround is to disable the check using a `OPENDKIM_RequireSafeKeys=no` environment variable.

## Legacy: delegated user tokens (refresh_token mode)

This is the original way the image worked and is still available when no
`OAUTH_*` variables are set. sasl-xoauth2 refreshes a user token itself, which
requires a refresh token obtained interactively and, for Microsoft 365, fails
once MFA is enforced on the account. Prefer the app-only mode above.

### Example configuration

```
environment:
...
  - POSTFIXMASTER_submission__inet="submission inet n - y - - smtpd -o syslog_name=postfix/submission"
  - POSTFIX_smtpd_tls_security_level="may"
  - POSTFIX_smtpd_reject_unlisted_recipient="no"
  - POSTFIX_myhostname="POSTFIX"
  - POSTFIX_smtpd_relay_restrictions="permit_mynetworks, permit_sasl_authenticated, check_relay_domains"
  - POSTFIX_smtp_use_tls="yes"
  - POSTFIX_smtp_sasl_auth_enable="yes"
  - POSTFIX_smtp_sasl_security_options="noanonymous"
  - POSTFIX_smtp_sasl_mechanism_filter="xoauth2"
  - POSTFIX_smtp_tls_security_level="encrypt"
  - POSTFIX_smtp_tls_CAfile="/etc/ssl/certs/ca-certificates.crt"
  - POSTFIX_transport_maps="hash:/etc/postfix/transport"
  - POSTMAP_transport="*       relay:[smtp.gmail.com]:587"
  - POSTFIX_smtp_sasl_password_maps="hash:/etc/postfix/sasl_passwd"
  - |
    POSTMAP_sasl_passwd=
    [smtp.gmail.com]:587   user@gmail.com:/etc/tokens/sender.tokens.json
...
```

Then initialize the SASL-XOAuth2 configuration files in the container
from known-working existing files with these commands. (This is just
one way to do it; a bind mount of `/etc/tokens/sender.tokens.json` also
works, and the entrypoint copies it into and out of the Postfix chroot.)
```
...
  docker cp sasl-xoauth2.conf postfix:/tmp
  docker exec -it --workdir /root --user root postfix bash -c "cat /tmp/sasl-xoauth2.conf > /etc/sasl-xoauth2.conf"
  docker exec -it --workdir /root --user root postfix bash -c "chown root:postfix /etc/sasl-xoauth2.conf"
  docker exec -it --workdir /root --user root postfix bash -c "chmod 0640 /etc/sasl-xoauth2.conf"
  docker exec -it --workdir /root --user root postfix bash -c "mkdir  /etc/tokens  /var/spool/postfix/etc/tokens"
  docker cp sender.tokens.json postfix:/etc/tokens/sender.tokens.json
  docker exec -it --workdir /root --user root postfix bash -c "chown postfix:postfix /etc/tokens/sender.tokens.json"
  docker exec -it --workdir /root --user root postfix bash -c "chmod 0640 /etc/tokens/sender.tokens.json"
  docker exec -it --workdir /root --user root postfix bash -c "cp -p /etc/tokens/sender.tokens.json /var/spool/postfix/etc/tokens/sender.tokens.json"
  docker exec -it --workdir /root --user root postfix bash -c "cp -p /etc/ssl/certs/ca-certificates.crt /var/spool/postfix/etc/ssl/certs/ca-certificates.crt"
  docker exec -it --workdir /root --user root postfix bash -c "rm -f /tmp/sasl-xoauth2.conf"
...
```

#### Hint for how to create the tokens

This is how the tokens were generated on another host (Your Mileage May Vary).
This is only a _HINT_!  Please read the documentation (enumerated above).
```
sasl-xoauth2-tool get-token gmail --client-id="55XXXXXXXXXX-pXXXXXkqXXXXXXXXXXXXXXXXXXXXXXX.apps.googleusercontent.com" --client-secret="GOCSPX-XXXXXXXXXXXXXXXXXXXXXXXXXXXX" --scope="https://mail.google.com/" tokens-stored-in-this-file
```

For Microsoft 365: `sasl-xoauth2-tool get-token outlook --client-id=... --tenant=<tenant id> --use-device-flow sender.tokens.json`, then add
`"token_endpoint": "https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/token"` to the file.

## SPF
When sending email using your own SMTP server it is probably a good idea
to setup [SPF](https://en.wikipedia.org/wiki/Sender_Policy_Framework) for the
domain you're sending from.

## DKIM
To enable [DKIM](https://en.wikipedia.org/wiki/DomainKeys_Identified_Mail),
specify a whitespace-separated list of domains in the environment variable
`OPENDKIM_DOMAINS`. The default DKIM selector is "mail", but can be changed to
"`<selector>`" using the syntax `OPENDKIM_DOMAINS=<domain>=<selector>`.

At container start, RSA key pairs will be generated for each domain unless the
file `/etc/opendkim/keys/<domain>/<selector>.private` exists. If you want the
keys to persist indefinitely, make sure to mount a volume for
`/etc/opendkim/keys`, otherwise they will be destroyed when the container is
removed.

DNS records to configure can be found in the container log or by running `docker exec <container> sh -c 'cat /etc/opendkim/keys/*/*.txt` you should see something like this:
```bash
$ docker exec 7996454b5fca sh -c 'cat /etc/opendkim/keys/*/*.txt'

mail._domainkey.smtp.domain.tld. IN	TXT	( "v=DKIM1; h=sha256; k=rsa; "
	  "p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0Dx7wLGPFVaxVQ4TGym/eF89aQ8oMxS9v5BCc26Hij91t2Ci8Fl12DHNVqZoIPGm+9tTIoDVDFEFrlPhMOZl8i4jU9pcFjjaIISaV2+qTa8uV1j3MyByogG8pu4o5Ill7zaySYFsYB++cHJ9pjbFSC42dddCYMfuVgrBsLNrvEi3dLDMjJF5l92Uu8YeswFe26PuHX3Avr261n"
	  "j5joTnYwat4387VEUyGUnZ0aZxCERi+ndXv2/wMJ0tizq+a9+EgqIb+7lkUc2XciQPNuTujM25GhrQBEKznvHyPA6fHsFheymOuB763QpkmnQQLCxyLygAY9mE/5RY+5Q6J9oDOQIDAQAB" )  ; ----- DKIM key mail for smtp.domain.tld
```

## License
postfix-relay-m365 is licensed under the MIT license. See [LICENSE](LICENSE) for the
full license text.
