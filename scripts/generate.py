#!/usr/bin/env python3
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

D = Path(os.environ.get("DATA_DIR", "/data")); D.mkdir(parents=True, exist_ok=True)
C = Path(os.environ.get("XRAY_CONFIG", "/etc/xray/config.json"))
UUID = os.environ["UUID"].strip(); PRIVATE_KEY = os.environ["PRIVATE_KEY"].strip(); PUBLIC_KEY = os.environ["PUBLIC_KEY"].strip(); PUBLIC_DOMAIN = os.environ["PUBLIC_DOMAIN"].strip(); APP_PORT = 8080
TCP_HOST = (os.environ.get("RAILWAY_TCP_PROXY_DOMAIN") or "").strip(); TCP_PORT_RAW = (os.environ.get("RAILWAY_TCP_PROXY_PORT") or "").strip()
if not PUBLIC_DOMAIN: raise SystemExit("FATAL: RAILWAY_PUBLIC_DOMAIN/PUBLIC_DOMAIN required")
if not TCP_HOST or not TCP_PORT_RAW: raise SystemExit("FATAL: RAILWAY_TCP_PROXY_DOMAIN/PORT required")
try: TCP_PORT = int(TCP_PORT_RAW)
except ValueError: raise SystemExit("FATAL: invalid RAILWAY_TCP_PROXY_PORT")
if not 1 <= TCP_PORT <= 65535: raise SystemExit("FATAL: invalid TCP proxy port")
FP = os.environ.get("REALITY_FINGERPRINT", "chrome").strip() or "chrome"
RAW_SNI = os.environ.get("REALITY_RAW_SNI", "www.cloudflare.com").strip().lower().rstrip(".") or "www.cloudflare.com"
XHTTP_SNI = os.environ.get("REALITY_XHTTP_SNI", "www.apple.com").strip().lower().rstrip(".") or "www.apple.com"
GRPC_SNI = os.environ.get("REALITY_GRPC_SNI", "www.bing.com").strip().lower().rstrip(".") or "www.bing.com"
RAW_TARGET = os.environ.get("REALITY_RAW_TARGET", "www.cloudflare.com:443").strip() or "www.cloudflare.com:443"
XHTTP_TARGET = os.environ.get("REALITY_XHTTP_TARGET", "www.apple.com:443").strip() or "www.apple.com:443"
GRPC_TARGET = os.environ.get("REALITY_GRPC_TARGET", "www.bing.com:443").strip() or "www.bing.com:443"
XPATH = os.environ.get("XHTTP_PATH", "/xhttp").strip() or "/xhttp"
WSPATH = os.environ.get("WS_PATH", "/ws").strip() or "/ws"
GRPC_SERVICE_NAME = os.environ.get("GRPC_SERVICE_NAME", "grpc-service").strip() or "grpc-service"
TOKEN_FILE = D / "subscription_token.txt"
if not TOKEN_FILE.is_file(): raise SystemExit("FATAL: persisted subscription token is missing")
TOKEN = TOKEN_FILE.read_text().strip()
if not TOKEN: raise SystemExit("FATAL: persisted subscription token is empty")

def env_first(*names):
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value: return value
    return ""
CF_TOKEN = env_first("CLOUDFLARE_TUNNEL_TOKEN", "CF_TUNNEL_TOKEN", "TUNNEL_TOKEN")
CF_ID = env_first("CLOUDFLARE_TUNNEL_ID", "CF_TUNNEL_ID", "TUNNEL_ID")
CF_HOST = env_first("CLOUDFLARE_PUBLIC_HOSTNAME", "CF_PUBLIC_HOSTNAME").lower()
CF_ORIGIN_RAW = env_first("CLOUDFLARE_ORIGIN_SERVICE", "CF_ORIGIN_SERVICE")
CF_PORT_RAW = env_first("CLOUDFLARE_XHTTP_PORT", "CF_XHTTP_PORT")
CF_PATH = env_first("CLOUDFLARE_XHTTP_PATH", "CF_XHTTP_PATH")
cf_vars = (CF_TOKEN, CF_ID, CF_HOST, CF_ORIGIN_RAW, CF_PORT_RAW, CF_PATH)
if any(cf_vars) and not all(cf_vars): raise SystemExit("FATAL: incomplete Cloudflare configuration; provide all 6 variables or none")
CF_ENABLED = all(cf_vars); CF_PORT = None; CF_ORIGIN = ""
if CF_ENABLED:
    try: CF_PORT = int(CF_PORT_RAW)
    except ValueError: raise SystemExit("FATAL: CLOUDFLARE_XHTTP_PORT is not an integer")
    if not 1 <= CF_PORT <= 65535: raise SystemExit("FATAL: CLOUDFLARE_XHTTP_PORT outside 1-65535")
    if CF_PORT in (8080,10086,10087,10088,10089,10090): raise SystemExit("FATAL: CLOUDFLARE_XHTTP_PORT conflicts with internal port")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", CF_HOST): raise SystemExit("FATAL: invalid Cloudflare hostname")
    if not CF_PATH.startswith("/"): raise SystemExit("FATAL: CLOUDFLARE_XHTTP_PATH must start with /")
    CF_ORIGIN = f"http://127.0.0.1:{CF_PORT}"
ids_file = D / "reality_short_ids.json"
try: ids = json.loads(ids_file.read_text())
except Exception as exc: raise SystemExit(f"FATAL: persisted REALITY short IDs are unreadable: {exc}")
if not isinstance(ids, list) or len(ids) != 3 or any(not re.fullmatch(r"[0-9a-fA-F]{2,32}", str(x)) for x in ids): raise SystemExit("FATAL: persisted REALITY short IDs are invalid")
ids = [str(x) for x in ids]
def reality(tag, port, network, sni, target, sid, flow="", service_name=""):
    client = {"id": UUID, "level": 0}
    if flow: client["flow"] = flow
    ss = {"network": network, "security": "reality", "realitySettings": {"show": False, "target": target, "serverNames": [sni], "privateKey": PRIVATE_KEY, "shortIds": [sid]}}
    if network == "xhttp": ss["xhttpSettings"] = {"path": XPATH, "mode": "auto"}
    elif network == "grpc": ss["grpcSettings"] = {"serviceName": service_name, "multiMode": False}
    return {"tag": tag, "listen": "127.0.0.1", "port": port, "protocol": "vless", "settings": {"clients": [client], "decryption": "none"}, "streamSettings": ss}
xhttp_tls = {"tag": "vless-xhttp-tls", "listen": "127.0.0.1", "port": 10086, "protocol": "vless", "settings": {"clients": [{"id": UUID, "level": 0}], "decryption": "none"}, "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": {"path": XPATH, "mode": "auto"}}}
ws_tls = {"tag": "vless-ws-tls", "listen": "127.0.0.1", "port": 10090, "protocol": "vless", "settings": {"clients": [{"id": UUID, "level": 0}], "decryption": "none"}, "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": WSPATH, "headers": {}}}}
raw = reality("vless-reality-vision", 10087, "tcp", RAW_SNI, RAW_TARGET, ids[0], "xtls-rprx-vision")
xhttp_reality = reality("vless-xhttp-reality", 10088, "xhttp", XHTTP_SNI, XHTTP_TARGET, ids[1])
grpc_reality = reality("vless-grpc-reality", 10089, "grpc", GRPC_SNI, GRPC_TARGET, ids[2], service_name=GRPC_SERVICE_NAME)
inbounds = [xhttp_tls, ws_tls, raw, xhttp_reality, grpc_reality]
if CF_ENABLED: inbounds.append({"tag": "vless-xhttp-cloudflare", "listen": "127.0.0.1", "port": CF_PORT, "protocol": "vless", "settings": {"clients": [{"id": UUID, "level": 0}], "decryption": "none"}, "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": {"path": CF_PATH, "mode": "auto"}}})
config = {"log": {"loglevel": os.environ.get("XRAY_LOGLEVEL", "warning")}, "policy": {"levels": {"0": {"handshake": 8, "connIdle": 900, "uplinkOnly": 2, "downlinkOnly": 5}}}, "inbounds": inbounds, "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}]}
C.write_text(json.dumps(config, indent=2) + "\n")
def q(d): return urllib.parse.urlencode({k: str(v) for k, v in d.items() if v not in (None, "")}, safe="")
def link(host, port, params, name): return f'vless://{UUID}@{host}:{port}?{q(params)}#{urllib.parse.quote(name, safe="")}'
lines = [link(PUBLIC_DOMAIN, 443, {"encryption":"none","security":"tls","sni":PUBLIC_DOMAIN,"fp":FP,"alpn":"h2,http/1.1","type":"xhttp","path":XPATH,"mode":"auto"}, "VLESS XHTTP TLS · Railway Domain"), link(PUBLIC_DOMAIN, 443, {"encryption":"none","security":"tls","sni":PUBLIC_DOMAIN,"fp":FP,"alpn":"http/1.1","type":"ws","path":WSPATH,"host":PUBLIC_DOMAIN}, "VLESS WS TLS · Railway Domain"), link(TCP_HOST, TCP_PORT, {"encryption":"none","flow":"xtls-rprx-vision","security":"reality","sni":RAW_SNI,"fp":FP,"pbk":PUBLIC_KEY,"sid":ids[0],"type":"tcp"}, "VLESS RAW REALITY Vision · TCP Proxy"), link(TCP_HOST, TCP_PORT, {"encryption":"none","security":"reality","sni":XHTTP_SNI,"fp":FP,"alpn":"h2","pbk":PUBLIC_KEY,"sid":ids[1],"type":"xhttp","path":XPATH,"mode":"auto"}, "VLESS XHTTP REALITY · TCP Proxy"), link(TCP_HOST, TCP_PORT, {"encryption":"none","security":"reality","sni":GRPC_SNI,"fp":FP,"alpn":"h2","pbk":PUBLIC_KEY,"sid":ids[2],"type":"grpc","serviceName":GRPC_SERVICE_NAME,"mode":"gun"}, "VLESS gRPC REALITY · TCP Proxy")]
if CF_ENABLED: lines.append(link(CF_HOST, 443, {"encryption":"none","security":"tls","sni":CF_HOST,"fp":FP,"alpn":"h2,http/1.1","type":"xhttp","host":CF_HOST,"path":CF_PATH,"mode":"auto"}, "VLESS XHTTP TLS · Cloudflare Tunnel"))
NODE_COUNT = len(lines)
if NODE_COUNT not in (5,6): raise SystemExit(f"FATAL: invalid node count: {NODE_COUNT}")
BUILD_ID = os.environ.get("BUILD_ID", os.environ.get("RELEASE", "runtime-derived")).strip() or "runtime-derived"
rf = D / "runtime.json"; prev = {}
if rf.is_file():
    try: prev = json.loads(rf.read_text())
    except Exception: prev = {}
pts = prev.get("tcp_proxy",{}) or {}; prev_public = str(prev.get("public_domain","")); prev_tcp = f"{pts.get('domain','')}:{pts.get('port','')}" if (pts.get("domain") or pts.get("port")) else ""; current_tcp = f"{TCP_HOST}:{TCP_PORT}"
state = "initial" if not prev else ("unchanged" if prev_public == PUBLIC_DOMAIN and prev_tcp == current_tcp else "changed")
runtime = {"schema":26,"build":BUILD_ID,"architecture":"fixed-node-order-1-xhttp-tls-2-ws-tls-3-raw-reality-vision-4-xhttp-reality-5-grpc-reality-6-cloudflare-xhttp-tls","cloudflare":{"enabled":CF_ENABLED,"transport":"xhttp","public_tls":True,"origin_protocol":"http","token_configured":bool(CF_TOKEN),"tunnel_id_configured":bool(CF_ID),"public_hostname":CF_HOST if CF_ENABLED else "","origin_service":CF_ORIGIN if CF_ENABLED else "","xhttp_port":CF_PORT if CF_ENABLED else None,"xhttp_path":CF_PATH if CF_ENABLED else ""},"nodes":{"count":NODE_COUNT,"distribution":{"01":"domain-xhttp-tls","02":"domain-ws-tls","03":"raw-reality-vision","04":"xhttp-reality","05":"grpc-reality",**({"06":"cloudflare-xhttp-tls"} if CF_ENABLED else {})}},"application_port":APP_PORT,"public_domain":PUBLIC_DOMAIN,"tcp_proxy":{"domain":TCP_HOST,"port":TCP_PORT,"application_port":APP_PORT},"railway_networking":{"source":"current-deployment-environment","authoritative":True,"state":state,"previous_public_domain":prev_public,"current_public_domain":PUBLIC_DOMAIN,"previous_tcp_proxy":prev_tcp,"current_tcp_proxy":current_tcp},"routes":{"domain_xhttp_tls":{"port":10086,"path":XPATH},"domain_ws_tls":{"port":10090,"path":WSPATH},"raw_reality_vision":{"sni":RAW_SNI,"port":10087,"short_id":ids[0]},"xhttp_reality":{"sni":XHTTP_SNI,"port":10088,"short_id":ids[1]},"grpc_reality":{"sni":GRPC_SNI,"port":10089,"short_id":ids[2],"service_name":GRPC_SERVICE_NAME},**({"cloudflare_xhttp_tls":{"host":CF_HOST,"port":CF_PORT,"path":CF_PATH,"origin":CF_ORIGIN}} if CF_ENABLED else {})}}
runtime["fingerprint"] = hashlib.sha256(json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
(D/"runtime.json").write_text(json.dumps(runtime, indent=2)+"\n"); (D/"state.json").write_text(json.dumps(runtime, indent=2)+"\n"); (D/"subscription.txt.tmp").write_text("\n".join(lines)+"\n"); os.replace(D/"subscription.txt.tmp", D/"subscription.txt")
subscription_url = f"https://{PUBLIC_DOMAIN}/sub/{TOKEN}"
(D/"subscription_url.txt.tmp").write_text(subscription_url + "\n")
os.replace(D/"subscription_url.txt.tmp", D/"subscription_url.txt")
try: os.chmod(D/"subscription_url.txt", 0o600)
except OSError: pass
(D/"manifest.json").write_text(json.dumps({"schema":26,"build":BUILD_ID,"node_count":NODE_COUNT,"application_port":APP_PORT,"cloudflare_xhttp_enabled":CF_ENABLED,"distribution":runtime["nodes"]["distribution"],"runtime_fingerprint":runtime["fingerprint"],"railway_networking_source":"current-deployment-environment","railway_networking_authoritative":True,"railway_networking_state":state}, indent=2)+"\n")
print(f"RELEASE={BUILD_ID}",flush=True); print(f"RUNTIME_FINGERPRINT={runtime['fingerprint']}",flush=True); print("RAILWAY_NETWORKING_SOURCE=current-deployment-environment",flush=True); print("RAILWAY_NETWORKING_AUTHORITATIVE=true",flush=True); print(f"RAILWAY_NETWORKING={state}",flush=True); print(f"RAILWAY_CURRENT_PUBLIC={PUBLIC_DOMAIN}",flush=True); print(f"RAILWAY_CURRENT_TCP={current_tcp}",flush=True); print(f"CLOUDFLARE_XHTTP={'enabled' if CF_ENABLED else 'disabled'}",flush=True); print(f"SUBSCRIPTION_INVARIANT={NODE_COUNT}",flush=True); print("NODE_ORDER=1:domain-xhttp-tls,2:domain-ws-tls,3:raw-reality-vision,4:xhttp-reality,5:grpc-reality,6:cloudflare-xhttp-tls",flush=True); print(f"NODES={NODE_COUNT}",flush=True); print("SUBSCRIPTION_URL_FILE=/data/subscription_url.txt",flush=True)
subprocess.run(["python3", str(Path(__file__).with_name("subscription-contract.py"))], check=True)
