#!/usr/bin/env python3
"""Generate runtime metadata from the current Railway deployment environment."""
import hashlib
import json
import os
from pathlib import Path

D = Path(os.environ.get("DATA_DIR", "/data")); D.mkdir(parents=True, exist_ok=True)
public = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
tcp_host = os.environ.get("RAILWAY_TCP_PROXY_DOMAIN", "").strip()
tcp_port = os.environ.get("RAILWAY_TCP_PROXY_PORT", "").strip()
def env_first(*names):
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""

# Canonical Cloudflare XHTTP variables, with legacy aliases accepted for migration.
cf = {
    "CLOUDFLARE_TUNNEL_TOKEN": env_first("CLOUDFLARE_TUNNEL_TOKEN", "CF_TUNNEL_TOKEN", "TUNNEL_TOKEN"),
    "CLOUDFLARE_TUNNEL_ID": env_first("CLOUDFLARE_TUNNEL_ID", "CF_TUNNEL_ID", "TUNNEL_ID"),
    "CLOUDFLARE_PUBLIC_HOSTNAME": env_first("CLOUDFLARE_PUBLIC_HOSTNAME", "CF_PUBLIC_HOSTNAME"),
    "CLOUDFLARE_ORIGIN_SERVICE": env_first("CLOUDFLARE_ORIGIN_SERVICE", "CF_ORIGIN_SERVICE"),
    "CLOUDFLARE_XHTTP_PORT": env_first("CLOUDFLARE_XHTTP_PORT", "CF_XHTTP_PORT"),
    "CLOUDFLARE_XHTTP_PATH": env_first("CLOUDFLARE_XHTTP_PATH", "CF_XHTTP_PATH"),
}
cf_count = sum(bool(v) for v in cf.values())
if cf_count not in (0, len(cf)):
    missing = ",".join(k for k,v in cf.items() if not v)
    raise SystemExit(f"FATAL: incomplete Cloudflare XHTTP configuration; missing={missing}")
cf_enabled = cf_count == len(cf)

nodes = []
if public:
    nodes.append({"id":"node-01","name":os.environ.get("NODE_01_NAME","Node 01").strip() or "Node 01","transport":"xhttp","security":"tls","endpoint_source":"railway_public_domain","endpoint":f"{public}:443"})
    nodes.append({"id":"node-02","name":os.environ.get("NODE_02_NAME","Node 02").strip() or "Node 02","transport":"ws","security":"tls","endpoint_source":"railway_public_domain","endpoint":f"{public}:443"})
if tcp_host and tcp_port:
    raw_sni=os.environ.get("REALITY_RAW_SNI","www.cloudflare.com").strip(); xhttp_sni=os.environ.get("REALITY_XHTTP_SNI","www.apple.com").strip(); grpc_sni=os.environ.get("REALITY_GRPC_SNI","www.bing.com").strip()
    nodes.extend([
        {"id":"node-03","name":os.environ.get("NODE_03_NAME","Node 03").strip() or "Node 03","transport":"tcp","security":"reality","flow":"xtls-rprx-vision","sni":raw_sni,"endpoint_source":"railway_tcp_proxy","endpoint":f"{tcp_host}:{tcp_port}"},
        {"id":"node-04","name":os.environ.get("NODE_04_NAME","Node 04").strip() or "Node 04","transport":"xhttp","security":"reality","sni":xhttp_sni,"endpoint_source":"railway_tcp_proxy","endpoint":f"{tcp_host}:{tcp_port}"},
        {"id":"node-05","name":os.environ.get("NODE_05_NAME","Node 05").strip() or "Node 05","transport":"grpc","security":"reality","sni":grpc_sni,"endpoint_source":"railway_tcp_proxy","endpoint":f"{tcp_host}:{tcp_port}"},
    ])
if cf_enabled:
    nodes.append({"id":"node-06","name":os.environ.get("NODE_06_NAME","Node 06").strip() or "Node 06","transport":"xhttp","security":"tls","endpoint_source":"cloudflare_tunnel","endpoint":f"{cf['CLOUDFLARE_PUBLIC_HOSTNAME']}:443","path":cf["CLOUDFLARE_XHTTP_PATH"],"origin_protocol":"http"})

policy={"node_count":len(nodes),"cloudflare_configured":cf_enabled,"cloudflare_transport":"xhttp","networking_source":"current-deployment-environment","networking_authoritative":True,"tcp_proxy_expected_target":8080,"names_source":"runtime-config-or-default-node-id","endpoints_source":"current-railway-environment"}
manifest={"schema":3,"kind":"runtime-deployment-manifest","project":{"name":os.environ.get("PROJECT_NAME","").strip() or None,"release":os.environ.get("RELEASE","").strip() or None},"policy":policy,"nodes":nodes,"capabilities":{"single_gateway":True,"sni_routing":True,"dynamic_railway_networking":True,"subscription_generation":True,"uuid_invariant":True,"cloudflare_tunnel":cf_enabled,"cloudflare_xhttp":cf_enabled}}
manifest["fingerprint"]=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(",",":")).encode()).hexdigest()
(D/"runtime-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
print(f"RUNTIME_NODE_COUNT={len(nodes)}")
print(f"CLOUDFLARE_CONFIG_STATE={'enabled' if cf_enabled else 'disabled'}")
print(f"RUNTIME_CLOUDFLARE={'enabled' if cf_enabled else 'disabled'}")
print(f"RUNTIME_CLOUDFLARE_TRANSPORT={'xhttp' if cf_enabled else 'disabled'}")
print("RAILWAY_NETWORKING_SOURCE=current-deployment-environment")
print("RAILWAY_NETWORKING_AUTHORITATIVE=true")
print("RAILWAY_TCP_PROXY_EXPECTED_TARGET=8080")
print(f"RUNTIME_MANIFEST={D/'runtime-manifest.json'}")
print(f"RUNTIME_MANIFEST_FINGERPRINT={manifest['fingerprint']}")
for n in nodes: print(f"NODE_DISCOVERED={n['id']} name={n['name']} transport={n['transport']} security={n['security']} endpoint={n['endpoint']}")
