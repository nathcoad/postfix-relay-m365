FROM ubuntu:24.04
ENV DEBCONF_NOWARNINGS=yes
ENV DEBIAN_FRONTEND=noninteractive
#ENV DEBIAN_PRIORITY=critical
LABEL org.opencontainers.image.title="postfix-relay-m365" \
      org.opencontainers.image.description="Postfix SMTP relay for Microsoft 365 using app-only OAuth 2.0 (client credentials) with sasl-xoauth2" \
      org.opencontainers.image.source="https://github.com/nathcoad/postfix-relay-m365" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Nathan Coad, Patrick Hess phesster@gmail.com"

#
# Adaptation of https://hub.docker.com/r/mwader/postfix-relay/ and
#    https://github.com/tarickb/sasl-xoauth2
#

###
### sasl-xoauth2 comes from the Launchpad PPA
### https://launchpad.net/~sasl-xoauth2/+archive/ubuntu/stable
### The PPA signing key (2E733F026005F791) is vendored in etc/apt/keyrings so the
### build never depends on a keyserver.
###
COPY etc/apt/keyrings/sasl-xoauth2.gpg /etc/apt/keyrings/sasl-xoauth2.gpg

RUN \
  echo "APT::Install-Suggests 0;\nAPT::Install-Recommends 0;" | tee /etc/apt/apt.conf.d/00-no-install-recommends && \
  echo "path-exclude=/usr/share/locale/*\npath-exclude=/usr/share/man/*\npath-exclude=/usr/share/doc/*\n" | tee  /etc/dpkg/dpkg.cfg.d/01-nodoc && \
  apt-get update && \
  apt-get upgrade -y && \
  apt-get -y --no-install-recommends install \
    procps \
    postfix \
    libsasl2-modules \
    opendkim \
    opendkim-tools \
    ca-certificates \
    libcurl4t64 \
    libjsoncpp25 \
    sasl2-bin \
    libgcc-s1 \
    tzdata \
    netcat-openbsd \
    python3 \
    rsyslog && \
  echo "deb [signed-by=/etc/apt/keyrings/sasl-xoauth2.gpg] https://ppa.launchpadcontent.net/sasl-xoauth2/stable/ubuntu/ noble main" | tee /etc/apt/sources.list.d/sasl-xoauth2-ubuntu-stable-noble.list && \
  apt-get update && \
  apt-get -y --no-install-recommends install \
    sasl-xoauth2 && \
  apt-get -y clean && \
  apt-get -y autoremove && \
  rm -rf /var/lib/apt/lists/* /etc/rsyslog.conf && \
  update-ca-certificates && \
  mkdir -p /var/spool/postfix/etc/ssl/certs && \
  { cp -p /etc/ssl/certs/ca-certificates.crt /var/spool/postfix/etc/ssl/certs/ || /bin/true ; } && \
  mkdir -p /etc/opendkim/keys && \
  sed -i ' s,-name,\\( -name, ' /usr/lib/postfix/configure-instance.sh && \
  sed -i ' s,-not,-o -name \\\*.crt \\) -not, ' /usr/lib/postfix/configure-instance.sh

COPY etc/sasl-xoauth2.conf /etc/sasl-xoauth2.conf
COPY etc/tokens/sender.tokens.json /var/spool/postfix/etc/tokens/sender.tokens.json
COPY etc/postfix/sasl_passwd /etc/postfix/sasl_passwd
COPY run /root/
# App-only OAuth token helper (oauth-token-helper / oauth-token-test) and the
# SMTP smoke test.  Python 3 standard library only.
COPY bin/oauth-token-helper bin/smtp-send-test /usr/local/bin/

RUN \
  chown postfix:postfix /var/spool/postfix/etc/tokens/sender.tokens.json && \
  chmod 0755 /usr/local/bin/oauth-token-helper /usr/local/bin/smtp-send-test && \
  ln -s oauth-token-helper /usr/local/bin/oauth-token-test


# Default config:
# Open relay, trust docker links for firewalling.
# Try to use TLS when sending to other smtp servers.
# No TLS for connecting clients, trust docker network to be safe
ENV \
  POSTFIX_myhostname=hostname \
  POSTFIX_mydestination=localhost \
  POSTFIX_mynetworks=0.0.0.0/0 \
  POSTFIX_smtp_tls_security_level=may \
  POSTFIX_smtpd_tls_security_level=none \
  OPENDKIM_Socket=inet:12301@localhost \
  OPENDKIM_Mode=sv \
  OPENDKIM_UMask=002 \
  OPENDKIM_Syslog=yes \
  OPENDKIM_InternalHosts="0.0.0.0/0, ::/0" \
  OPENDKIM_KeyTable=refile:/etc/opendkim/KeyTable \
  OPENDKIM_SigningTable=refile:/etc/opendkim/SigningTable \
  RSYSLOG_TIMESTAMP=no \
  RSYSLOG_LOG_TO_FILE=no

VOLUME ["/var/lib/postfix", "/var/mail", "/var/spool/postfix", "/etc/opendkim/keys"]

# Healthy = Postfix answers on port 25 (always listening, unlike submission/587
# which is optional) and, in client_credentials mode, an unexpired OAuth token
# is on disk.  In legacy refresh_token mode the status check is a no-op.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD printf "EHLO healthcheck\n" | nc 127.0.0.1 25 | grep -qE "^220.*ESMTP" && oauth-token-helper status -q

EXPOSE 25
CMD ["/root/run"]
