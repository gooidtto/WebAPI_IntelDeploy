#!/bin/sh
# Railway Universal Stable V5 - networking-safe startup.
# Identity is initialized once on the mounted Railway Volume and then reused forever.
# Runtime/network artifacts are regenerated from the current deployment environment.
set -eu
umask 077
REPOSITORY_IDENTITY="$(python3 /opt/xray/scripts/version.py 2>/dev/null || echo runtime)"
export RELEASE="$REPOSITORY_IDENTITY" BUILD_ID="$REPOSITORY_IDENTITY" SOURCE_BUILD="$REPOSITORY_IDENTITY"
D="${RAILWAY_VOLUME_MOUNT_PATH:-${DATA_DIR:-/data}}"
export DATA_DIR="$D"

python3 /opt/xray/scripts/identity-init.py
echo "NODE_IDENTITY_POLICY=INITIALIZE_ONCE_REUSE_FOREVER"

python3 /opt/xray/scripts/gateway.py >"$D/gateway.log" 2>&1 & GP=$!
tail -n 0 -F "$D/gateway.log" 2>/dev/null & GLP=$!

if [ -n "${RAILWAY_TOKEN:-}" ] || [ -n "${RAILWAY_API_TOKEN:-}" ]; then
  set +e
  python3 /opt/xray/scripts/railway_setup.py
  API_SETUP_RC=$?
  set -e
  if [ "$API_SETUP_RC" -eq 10 ]; then
    echo "RAILWAY_API_REDEPLOY=REQUESTED"
    exit 10
  fi
  if [ "$API_SETUP_RC" -ne 0 ]; then
    # Railway's injected runtime endpoints are authoritative. A control-plane
    # authorization/API failure must not kill a deployment that already has a
    # complete public domain and TCP proxy endpoint.
    RUNTIME_PUBLIC="${RAILWAY_PUBLIC_DOMAIN:-}"
    RUNTIME_TCP_HOST="${RAILWAY_TCP_PROXY_DOMAIN:-}"
    RUNTIME_TCP_PORT="${RAILWAY_TCP_PROXY_PORT:-}"
    RUNTIME_ENDPOINTS_OK=1
    case "$RUNTIME_TCP_PORT" in ''|*[!0-9]*) RUNTIME_ENDPOINTS_OK=0;; esac
    case "$RUNTIME_PUBLIC" in *.up.railway.app) :;; *) RUNTIME_ENDPOINTS_OK=0;; esac
    case "$RUNTIME_TCP_HOST" in *.proxy.rlwy.net) :;; *) RUNTIME_ENDPOINTS_OK=0;; esac
    if [ "$RUNTIME_ENDPOINTS_OK" -eq 1 ] && [ "$RUNTIME_TCP_PORT" -ge 1 ] && [ "$RUNTIME_TCP_PORT" -le 65535 ]; then
      echo "RAILWAY_API_SETUP=CONTINUE_RUNTIME_ENDPOINTS reason=control_plane_unavailable"
    else
      echo "FATAL: Railway API networking setup failed and runtime endpoints are incomplete" >&2
      exit 1
    fi
  fi
fi

PUBLIC_DOMAIN="${RAILWAY_PUBLIC_DOMAIN:-}"
TCP_HOST="${RAILWAY_TCP_PROXY_DOMAIN:-}"
TCP_PORT="${RAILWAY_TCP_PROXY_PORT:-}"
NETWORK_DISCOVERY_TIMEOUT="${RAILWAY_NETWORK_DISCOVERY_TIMEOUT:-180}"
NETWORK_DISCOVERY_INTERVAL="${RAILWAY_NETWORK_DISCOVERY_INTERVAL:-3}"
NETWORK_DISCOVERY_ELAPSED=0
while [ -z "$PUBLIC_DOMAIN" ] || [ -z "$TCP_HOST" ] || [ -z "$TCP_PORT" ]; do
  if [ "$NETWORK_DISCOVERY_ELAPSED" -ge "$NETWORK_DISCOVERY_TIMEOUT" ]; then
    [ -n "$PUBLIC_DOMAIN" ] || { echo "FATAL: RAILWAY_PUBLIC_DOMAIN unavailable; create Public Domain then Redeploy" >&2; exit 1; }
    [ -n "$TCP_HOST" ] && [ -n "$TCP_PORT" ] || { echo "FATAL: Railway TCP Proxy unavailable; create TCP Proxy -> target 8080 then Redeploy" >&2; exit 1; }
  fi
  PUBLIC_DOMAIN="${RAILWAY_PUBLIC_DOMAIN:-$PUBLIC_DOMAIN}"
  TCP_HOST="${RAILWAY_TCP_PROXY_DOMAIN:-$TCP_HOST}"
  TCP_PORT="${RAILWAY_TCP_PROXY_PORT:-$TCP_PORT}"
  if [ -z "$PUBLIC_DOMAIN" ] || [ -z "$TCP_HOST" ] || [ -z "$TCP_PORT" ]; then
    sleep "$NETWORK_DISCOVERY_INTERVAL"
    NETWORK_DISCOVERY_ELAPSED=$((NETWORK_DISCOVERY_ELAPSED + NETWORK_DISCOVERY_INTERVAL))
  fi
done
echo "RAILWAY_NETWORK_DISCOVERY=READY elapsed=${NETWORK_DISCOVERY_ELAPSED}s"
case "$TCP_PORT" in ''|*[!0-9]*) echo "FATAL: RAILWAY_TCP_PROXY_PORT must be numeric" >&2; exit 1;; esac
[ "$TCP_PORT" -ge 1 ] && [ "$TCP_PORT" -le 65535 ] || { echo "FATAL: Railway TCP Proxy port out of range" >&2; exit 1; }

for f in "$D/runtime.json" "$D/state.json" "$D/manifest.json" "$D/runtime-manifest.json" "$D/subscription.txt" "$D/subscription.txt.tmp" "$D/subscription_url.txt" "${XRAY_CONFIG:-$D/config.json}"; do
  rm -f "$f"
done

export DATA_DIR="$D" XRAY_CONFIG="${XRAY_CONFIG:-$D/config.json}" GATEWAY_PORT=8080
export RAILWAY_TCP_PROXY_TARGET_PORT=8080
export RAILWAY_NETWORKING_SOURCE=current-deployment-environment
export RAILWAY_NETWORKING_AUTHORITATIVE=true
export RAILWAY_TCP_PROXY_DOMAIN="$TCP_HOST" RAILWAY_TCP_PROXY_PORT="$TCP_PORT" PUBLIC_DOMAIN="$PUBLIC_DOMAIN"

UUID_FILE="$D/uuid.txt"
PRIV_FILE="$D/reality_private_key.txt"; PUB_FILE="$D/reality_public_key.txt"; TOKEN_FILE="$D/subscription_token.txt"
[ -s "$UUID_FILE" ] && [ -s "$PRIV_FILE" ] && [ -s "$PUB_FILE" ] && [ -s "$TOKEN_FILE" ] || {
  echo "FATAL: persisted identity set is incomplete after identity initialization" >&2
  exit 1
}
UUID="$(tr -d '[:space:]' <"$UUID_FILE")"
PRIVATE_KEY="$(tr -d '[:space:]' <"$PRIV_FILE")"
PUBLIC_KEY="$(tr -d '[:space:]' <"$PUB_FILE")"
TOKEN="$(tr -d '[:space:]' <"$TOKEN_FILE")"
[ -n "$UUID" ] && [ -n "$PRIVATE_KEY" ] && [ -n "$PUBLIC_KEY" ] && [ -n "$TOKEN" ] || {
  echo "FATAL: persisted identity contains an empty value" >&2
  exit 1
}
export UUID PRIVATE_KEY PUBLIC_KEY
export REALITY_RAW_SNI="${REALITY_RAW_SNI:-www.cloudflare.com}" REALITY_RAW_TARGET="${REALITY_RAW_TARGET:-www.cloudflare.com:443}"
export REALITY_FINGERPRINT="${REALITY_FINGERPRINT:-chrome}" REALITY_XHTTP_SNI="${REALITY_XHTTP_SNI:-www.apple.com}" REALITY_XHTTP_TARGET="${REALITY_XHTTP_TARGET:-www.apple.com:443}"
export REALITY_GRPC_SNI="${REALITY_GRPC_SNI:-www.bing.com}" REALITY_GRPC_TARGET="${REALITY_GRPC_TARGET:-www.bing.com:443}"
export GRPC_SERVICE_NAME="${GRPC_SERVICE_NAME:-grpc-service}" XHTTP_PATH="${XHTTP_PATH:-/xhttp}" WS_PATH="${WS_PATH:-/ws}"
CF_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-${CF_TUNNEL_TOKEN:-${TUNNEL_TOKEN:-}}}"; CF_ID="${CLOUDFLARE_TUNNEL_ID:-${CF_TUNNEL_ID:-${TUNNEL_ID:-}}}"; CF_HOST="${CLOUDFLARE_PUBLIC_HOSTNAME:-${CF_PUBLIC_HOSTNAME:-}}"; CF_ORIGIN="${CLOUDFLARE_ORIGIN_SERVICE:-${CF_ORIGIN_SERVICE:-}}"; CF_PORT="${CLOUDFLARE_XHTTP_PORT:-${CF_XHTTP_PORT:-}}"; CF_PATH="${CLOUDFLARE_XHTTP_PATH:-${CF_XHTTP_PATH:-}}"
export CLOUDFLARE_TUNNEL_TOKEN="$CF_TOKEN" CLOUDFLARE_TUNNEL_ID="$CF_ID" CLOUDFLARE_PUBLIC_HOSTNAME="$CF_HOST" CLOUDFLARE_ORIGIN_SERVICE="$CF_ORIGIN" CLOUDFLARE_XHTTP_PORT="$CF_PORT" CLOUDFLARE_XHTTP_PATH="$CF_PATH"
XP=""; CFP=""; cleanup(){ kill "$XP" "$GP" "$GLP" "$CFP" 2>/dev/null || true; wait "$XP" 2>/dev/null || true; wait "$GP" 2>/dev/null || true; wait "$GLP" 2>/dev/null || true; wait "$CFP" 2>/dev/null || true; }; trap cleanup INT TERM EXIT
sleep 1; kill -0 "$GP" 2>/dev/null || { echo "FATAL: gateway failed to bind 8080" >&2; tail -80 "$D/gateway.log" >&2 || true; exit 1; }; echo "GATEWAY_BIND_EARLY=PASS port=8080"; echo "HEALTH_ENDPOINT=PASS path=/health"
TCP_HOST="${RAILWAY_TCP_PROXY_DOMAIN}"; TCP_PORT="${RAILWAY_TCP_PROXY_PORT}"; TCP_CHECK_SECONDS="${TCP_PROXY_CHECK_SECONDS:-20}"; TCP_CHECK_INTERVAL="${TCP_PROXY_CHECK_INTERVAL:-2}"; TCP_PROXY_SETTLE_SECONDS="${TCP_PROXY_SETTLE_SECONDS:-30}"
tcp_proxy_probe(){ python3 - "$TCP_HOST" "$TCP_PORT" <<'PY2'
import socket,sys
try: infos=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)
except Exception: raise SystemExit(2)
for fam,typ,proto,_,addr in infos:
 s=socket.socket(fam,typ,proto); s.settimeout(2.0)
 try: s.connect(addr); s.close(); raise SystemExit(0)
 except OSError:
  try:s.close()
  except Exception:pass
raise SystemExit(1)
PY2
}
TCP_PROXY_DNS=UNKNOWN; TCP_PROXY_CONNECTIVITY=UNKNOWN; TCP_PROXY_PROBE_ELAPSED=0; _probe=0
while [ "$_probe" -lt "$TCP_CHECK_SECONDS" ]; do if tcp_proxy_probe; then TCP_PROXY_DNS=READY; TCP_PROXY_CONNECTIVITY=READY; TCP_PROXY_PROBE_ELAPSED="$_probe"; break; fi; TCP_PROXY_DNS=READY; TCP_PROXY_CONNECTIVITY=WAITING; _probe=$((_probe + TCP_CHECK_INTERVAL)); [ "$_probe" -lt "$TCP_CHECK_SECONDS" ] && sleep "$TCP_CHECK_INTERVAL"; done
if [ "$TCP_PROXY_CONNECTIVITY" != "READY" ]; then TCP_PROXY_CONNECTIVITY=UNCONFIRMED; TCP_PROXY_PROBE_ELAPSED="$TCP_CHECK_SECONDS"; fi
if [ "$TCP_PROXY_CONNECTIVITY" = "READY" ] && [ "$TCP_PROXY_SETTLE_SECONDS" -gt 0 ]; then echo "TCP_PROXY_SETTLE=WAIT seconds=${TCP_PROXY_SETTLE_SECONDS}"; sleep "$TCP_PROXY_SETTLE_SECONDS"; echo "TCP_PROXY_SETTLE=COMPLETE"; fi
echo "TCP_PROXY_DOMAIN=${TCP_HOST}"; echo "TCP_PROXY_PORT=${TCP_PORT}"; echo "TCP_PROXY_TARGET_PORT=8080"; echo "TCP_PROXY_DNS=${TCP_PROXY_DNS}"; echo "TCP_PROXY_CONNECTIVITY=${TCP_PROXY_CONNECTIVITY}"; echo "TCP_PROXY_PROBE_SECONDS=${TCP_PROXY_PROBE_ELAPSED}"; echo "TCP_PROXY_SELF_CONNECTIVITY=${TCP_PROXY_CONNECTIVITY}"; echo "TCP_PROXY_EXTERNAL_PATH=UNVERIFIED"; echo "NETWORKING_STATE=READY"
python3 /opt/xray/scripts/runtime-manifest.py; python3 /opt/xray/scripts/generate.py

SUBSCRIPTION_URL_FILE="$D/subscription_url.txt"
EXPECTED_SUBSCRIPTION_URL="https://${PUBLIC_DOMAIN}/sub/${TOKEN}"
printf '%s\n' "$EXPECTED_SUBSCRIPTION_URL" >"$SUBSCRIPTION_URL_FILE.tmp"
chmod 0600 "$SUBSCRIPTION_URL_FILE.tmp"
mv -f "$SUBSCRIPTION_URL_FILE.tmp" "$SUBSCRIPTION_URL_FILE"
[ -s "$SUBSCRIPTION_URL_FILE" ] || { echo "FATAL: subscription_url.txt was not created" >&2; exit 1; }
[ "$(cat "$SUBSCRIPTION_URL_FILE")" = "$EXPECTED_SUBSCRIPTION_URL" ] || { echo "FATAL: subscription_url.txt content mismatch" >&2; exit 1; }
echo "SUBSCRIPTION_URL_FILE=PASS"

RUNTIME="$D/runtime.json"; [ -s "$RUNTIME" ] || { echo "FATAL: runtime state was not generated" >&2; exit 1; }; echo "RAILWAY_CURRENT_PUBLIC=$PUBLIC_DOMAIN"; echo "RAILWAY_CURRENT_TCP=$TCP_HOST:$TCP_PORT"; echo "RAILWAY_TCP_PROXY_TARGET_PORT=8080"
python3 - "$RUNTIME" "$D/subscription.txt" <<'PY'
import json,sys
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text()); lines=[x.strip() for x in Path(sys.argv[2]).read_text().splitlines() if x.strip()]; expected=int(r.get('nodes',{}).get('count',0)); tcp=r.get('tcp_proxy',{}); public=r.get('public_domain',''); assert expected in (5,6) and len(lines)==expected; assert lines[0].startswith('vless://') and ('@'+public+':443?') in lines[0]; assert ('@'+public+':443?') in lines[1]
for i in (2,3,4): assert ('@%s:%s?'%(tcp.get('domain'),tcp.get('port'))) in lines[i]
print('STARTUP_RUNTIME_INVARIANT=PASS nodes=%d tcp=%s:%s'%(expected,tcp.get('domain'),tcp.get('port')))
PY
xray run -test -config "$XRAY_CONFIG"; xray run -config "$XRAY_CONFIG" & XP=$!
CF_ENABLED="$(python3 - "$RUNTIME" <<'PY2'
import json,sys
print("1" if json.load(open(sys.argv[1])).get("cloudflare",{}).get("enabled") is True else "0")
PY2
)"; CF_PORT_STATE="$(python3 - "$RUNTIME" <<'PY2'
import json,sys
v=json.load(open(sys.argv[1])).get("cloudflare",{}).get("xhttp_port"); print(v if v is not None else "")
PY2
)"
if [ "$CF_ENABLED" = "1" ]; then cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:2000 run --token "$CF_TOKEN" >"$D/cloudflared.log" 2>&1 & CFP=$!; i=0; while :; do if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:2000/ready", timeout=1).read()' 2>/dev/null; then echo "CLOUDFLARED_READY=PASS"; break; fi; kill -0 "$CFP" 2>/dev/null || { echo "FATAL: cloudflared exited before readiness" >&2; tail -50 "$D/cloudflared.log" >&2 || true; exit 1; }; i=$((i+1)); [ "$i" -lt "${CLOUDFLARE_READY_TIMEOUT:-45}" ] || { echo "FATAL: cloudflared readiness timeout" >&2; tail -50 "$D/cloudflared.log" >&2 || true; exit 1; }; sleep 1; done; fi
wait_port(){ h="$1"; p="$2"; label="$3"; i=0; while :; do if python3 -c 'import socket,sys;s=socket.create_connection((sys.argv[1],int(sys.argv[2])),1);s.close()' "$h" "$p" 2>/dev/null; then echo "READY_CHECK=$label:$p"; return 0; fi; kill -0 "$XP" 2>/dev/null || { echo "FATAL: xray exited before $label:$p" >&2; exit 1; }; i=$((i+1)); [ "$i" -lt "${READY_TIMEOUT:-90}" ] || { echo "FATAL: readiness timeout $label:$p" >&2; exit 1; }; sleep 1; done; }
wait_port 127.0.0.1 10086 xhttp-http; wait_port 127.0.0.1 10090 ws-http; wait_port 127.0.0.1 10087 raw-reality-vision; wait_port 127.0.0.1 10088 xhttp-reality; wait_port 127.0.0.1 10089 grpc-reality; if [ "$CF_ENABLED" = "1" ]; then wait_port 127.0.0.1 "$CF_PORT_STATE" cloudflare-xhttp-origin; fi

echo "========== DEPLOYMENT SUMMARY =========="; echo "RELEASE=$BUILD_ID"; echo "NODE_IDENTITY_POLICY=INITIALIZE_ONCE_REUSE_FOREVER"; echo "RAILWAY_NETWORKING_SOURCE=current-deployment-environment"; echo "RAILWAY_NETWORKING_AUTHORITATIVE=true"; echo "RAILWAY_TCP_PROXY_TARGET_PORT=8080"; echo "GATEWAY_BIND_EARLY=PASS"; echo "NODES=$(python3 -c 'import json;print(json.load(open("'"$RUNTIME"'"))["nodes"]["count"])')"; echo "SUBSCRIPTION_CHECK=PASS"; echo "XRAY=READY"; if [ "$CF_ENABLED" = "1" ]; then echo "NODE6=VLESS_XHTTP_TLS_CLOUDFLARE"; echo "CLOUDFLARE_TUNNEL=READY"; fi; echo "========================================"

while true; do
  if ! kill -0 "$GP" 2>/dev/null; then echo "FATAL: Gateway process exited; restarting container" >&2; exit 1; fi
  if ! kill -0 "$XP" 2>/dev/null; then echo "FATAL: Xray process exited; restarting container" >&2; exit 1; fi
  sleep 2
done
