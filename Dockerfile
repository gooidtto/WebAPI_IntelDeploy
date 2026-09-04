# syntax=docker/dockerfile:1
# Xray is intentionally frozen for production stability. Upgrade only by an explicit, tested commit.
ARG XRAY_VERSION=26.3.27
ARG XRAY_IMAGE_DIGEST=sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7
ARG CLOUDFLARED_VERSION=2026.7.3
FROM ghcr.io/xtls/xray-core:${XRAY_VERSION}@${XRAY_IMAGE_DIGEST} AS xray
FROM cloudflare/cloudflared:${CLOUDFLARED_VERSION} AS cloudflared

FROM python:3.12-alpine3.22
ARG XRAY_VERSION
ARG XRAY_IMAGE_DIGEST
ARG CLOUDFLARED_VERSION
ENV XRAY_VERSION=${XRAY_VERSION} XRAY_IMAGE_DIGEST=${XRAY_IMAGE_DIGEST} CLOUDFLARED_VERSION=${CLOUDFLARED_VERSION} PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NODE_IDENTITY_POLICY=INITIALIZE_ONCE_REUSE_FOREVER

RUN apk add --no-cache openssl ca-certificates && mkdir -p /etc/xray /data /opt/xray/scripts /opt/xray/config /opt/xray/site
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY scripts/ /opt/xray/scripts/
COPY config/ /opt/xray/config/
COPY site/ /opt/xray/site/

# Build-time invariants. Networking is runtime-derived; node identity remains
# exclusively owned by identity-init.py on the mounted persistent volume.
RUN set -eu; \
    check() { label="$1"; shift; if "$@"; then echo "BUILD_CHECK ${label}=PASS"; else echo "BUILD_CHECK ${label}=FAIL" >&2; exit 1; fi; }; \
    check xray_version sh -c '/usr/local/bin/xray version | head -n 1 | grep -q "Xray 26.3.27"'; \
    check xray_digest_pinned sh -c 'test "${XRAY_IMAGE_DIGEST}" = "sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7"'; \
    check py_compile python3 -m py_compile /opt/xray/scripts/*.py; \
    check identity_init_present test -f /opt/xray/scripts/identity-init.py; \
    check subscription_contract_present test -f /opt/xray/scripts/subscription-contract.py; \
    check persistent_volume_guard grep -q 'not a mounted persistent volume' /opt/xray/scripts/identity-init.py; \
    check identity_reuse grep -q 'emit_identity_status("REUSED")' /opt/xray/scripts/identity-init.py; \
    check identity_fail_closed grep -q 'refusing to regenerate identity' /opt/xray/scripts/identity-init.py; \
    check identity_integrity_seal grep -q 'identity-integrity.json' /opt/xray/scripts/identity-init.py; \
    check subscription_token_sealed grep -q 'TOKEN_SEAL_MISMATCH' /opt/xray/scripts/subscription-contract.py; \
    check subscription_http_contract grep -q 'SUBSCRIPTION_HTTP_LOCAL=PASS' /opt/xray/scripts/subscription-contract.py; \
    check subscription_endpoint_contract grep -q 'SUBSCRIPTION_ENDPOINT_CONTRACT=PASS' /opt/xray/scripts/subscription-contract.py; \
    check subscription_no_secret_logs grep -q 'SUBSCRIPTION_SECRETS_EXPOSED=NO' /opt/xray/scripts/subscription-contract.py; \
    check boot_identity_init grep -q 'identity-init.py' /opt/xray/scripts/boot.sh; \
    check identity_policy grep -q 'NODE_IDENTITY_POLICY=INITIALIZE_ONCE_REUSE_FOREVER' /opt/xray/scripts/boot.sh; \
    check short_ids_immutable grep -q 'reality_short_ids.json' /opt/xray/scripts/generate.py; \
    check subscription_url_generate grep -q 'subscription_url.txt' /opt/xray/scripts/generate.py; \
    check subscription_url_boot_guard grep -q 'SUBSCRIPTION_URL_FILE="\$D/subscription_url.txt"' /opt/xray/scripts/boot.sh; \
    check subscription_url_expected_guard grep -q 'EXPECTED_SUBSCRIPTION_URL=' /opt/xray/scripts/boot.sh; \
    check subscription_contract_invoked grep -q 'subscription-contract.py' /opt/xray/scripts/generate.py; \
    check token_rotation_id grep -q 'SUBSCRIPTION_TOKEN_ROTATE_ID' /opt/xray/scripts/identity-init.py; \
    check token_rotation_strict_format grep -q 'ROTATION_ID_RE' /opt/xray/scripts/identity-init.py; \
    check token_rotation_state grep -q 'subscription-token-rotation.json' /opt/xray/scripts/identity-init.py; \
    check token_rotation_idempotent grep -q 'ALREADY_APPLIED' /opt/xray/scripts/identity-init.py; \
    check gateway_early grep -q 'GATEWAY_BIND_EARLY=PASS' /opt/xray/scripts/boot.sh; \
    check deployment_summary grep -q 'DEPLOYMENT SUMMARY' /opt/xray/scripts/boot.sh; \
    check gateway_json grep -q 'application/json' /opt/xray/scripts/gateway.py; \
    check cloudflared_ready grep -q 'CLOUDFLARED_READY=PASS' /opt/xray/scripts/boot.sh; \
    check cloudflare_generator grep -q 'vless-xhttp-cloudflare' /opt/xray/scripts/generate.py; \
    check xhttp_network grep -q 'type\":\"xhttp\"' /opt/xray/scripts/generate.py; \
    check cloudflare_xhttp_tls grep -q 'cloudflare-xhttp-tls' /opt/xray/scripts/generate.py; \
    check tcp_proxy_target grep -q 'tcp_proxy_expected_target\":8080' /opt/xray/scripts/runtime-manifest.py; \
    check networking_domain_count grep -q 'RAILWAY_API_PUBLIC_DOMAIN_CONFIG_COUNT=' /opt/xray/scripts/railway_setup.py; \
    check networking_domain_safe_mode grep -q 'Non-destructive' /opt/xray/scripts/railway_setup.py; \
    check networking_tcp_count grep -q 'RAILWAY_API_TCP_PROXY_CONFIG_COUNT=' /opt/xray/scripts/railway_setup.py; \
    check networking_tcp_safe_mode grep -q 'Do not destructively delete' /opt/xray/scripts/railway_setup.py; \
    check networking_runtime_fallback grep -q 'RAILWAY_API_SETUP=CONTINUE_RUNTIME_ENDPOINTS' /opt/xray/scripts/railway_setup.py; \
    check railway_auth_fallback grep -q 'BEARER_FALLBACK' /opt/xray/scripts/railway_setup.py; \
    check ws_transport_present grep -q '"network": "ws"' /opt/xray/scripts/generate.py; \
    check ws_subscription_present grep -q '"type":"ws"' /opt/xray/scripts/generate.py; \
    check ws_gateway_route grep -q 'http-websocket' /opt/xray/scripts/gateway.py; \
    check no_runtime_identity_generation sh -c '! grep -q "secrets.token_" /opt/xray/scripts/generate.py'; \
    check no_boot_identity_generation sh -c '! grep -q "xray uuid" /opt/xray/scripts/boot.sh && ! grep -q "xray x25519" /opt/xray/scripts/boot.sh'; \
    chmod 0755 /usr/local/bin/xray /usr/local/bin/cloudflared /opt/xray/scripts/*.sh /opt/xray/scripts/*.py; \
    chmod 0644 /opt/xray/config/* /opt/xray/site/*

RUN echo "SOURCE_BUILD=runtime-derived BUILD_ID=runtime-derived NODE6=VLESS_XHTTP_TLS_CLOUDFLARE NODE_IDENTITY=INITIALIZE_ONCE_REUSE_FOREVER XRAY=26.3.27 XRAY_PINNED=true SUBSCRIPTION_CONTRACT=ENFORCED"
EXPOSE 8080
RUN test -x /opt/xray/scripts/railway_setup.py && python3 -m py_compile /opt/xray/scripts/railway_setup.py
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()"
WORKDIR /opt/xray
ENTRYPOINT ["/opt/xray/scripts/boot.sh"]
