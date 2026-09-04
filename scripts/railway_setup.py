#!/usr/bin/env python3
"""Idempotent Railway runtime networking bootstrap.

Railway API is an optional control-plane enhancement. The runtime-provided
network variables remain authoritative for the running deployment. An API
failure is therefore non-fatal when the current public/TCP endpoints are
already complete and valid; it remains fatal when endpoints are unavailable.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://backboard.railway.com/graphql/v2"
TARGET_PORT = 8080
API_RETRIES = max(1, int(os.environ.get("RAILWAY_API_RETRIES", "3")))
API_RETRY_DELAY = max(1.0, float(os.environ.get("RAILWAY_API_RETRY_DELAY", "2.5")))
PROJECT_TOKEN = os.environ.get("RAILWAY_TOKEN", "").strip()
ACCOUNT_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "").strip()
PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "").strip()
AUTH_MODE = None


class ApiError(RuntimeError):
    pass


def runtime_endpoints_complete():
    public = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    host = os.environ.get("RAILWAY_TCP_PROXY_DOMAIN", "").strip()
    port = os.environ.get("RAILWAY_TCP_PROXY_PORT", "").strip()
    if not public or not host or not port or not public.endswith(".up.railway.app") or not host.endswith(".proxy.rlwy.net"):
        return False
    try:
        return 1 <= int(port) <= 65535
    except ValueError:
        return False


def _request_once(query, variables, mode, token):
    headers = {"Content-Type": "application/json", "User-Agent": "railway-universal-stable/5.6"}
    headers["Project-Access-Token" if mode == "project" else "Authorization"] = token if mode == "project" else f"Bearer {token}"
    req = urllib.request.Request(API_URL, data=json.dumps({"query": query, "variables": variables or {}}).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        err = ApiError(f"HTTP {exc.code}: {detail[:500]}")
        err.retryable = exc.code == 429 or 500 <= exc.code <= 599
        raise err
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        err = ApiError(f"request failed: {exc}")
        err.retryable = True
        raise err
    except Exception as exc:
        raise ApiError(f"request failed: {exc}")
    if body.get("errors"):
        raise ApiError(json.dumps(body["errors"], ensure_ascii=False)[:1200])
    return body.get("data") or {}


def _request(query, variables, mode, token):
    last = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            return _request_once(query, variables, mode, token)
        except ApiError as exc:
            last = exc
            if not getattr(exc, "retryable", False) or attempt >= API_RETRIES:
                raise
            print(f"RAILWAY_API_RETRY={attempt}/{API_RETRIES} delay={API_RETRY_DELAY:g}s reason=transient")
            time.sleep(API_RETRY_DELAY)
    raise last or ApiError("Railway API request failed")


def gql(query, variables=None):
    global AUTH_MODE
    variables = variables or {}
    if AUTH_MODE:
        token = PROJECT_TOKEN if AUTH_MODE == "project" else ACCOUNT_TOKEN
        if not token:
            raise ApiError(f"configured Railway auth mode {AUTH_MODE} has no token")
        return _request(query, variables, AUTH_MODE, token)
    errors = []
    if PROJECT_TOKEN:
        try:
            data = _request(query, variables, "project", PROJECT_TOKEN)
            AUTH_MODE = "project"
            return data
        except ApiError as exc:
            errors.append(exc)
    if ACCOUNT_TOKEN and ACCOUNT_TOKEN != PROJECT_TOKEN:
        try:
            data = _request(query, variables, "bearer", ACCOUNT_TOKEN)
            AUTH_MODE = "bearer"
            print("RAILWAY_API_AUTH=BEARER_FALLBACK")
            return data
        except ApiError as exc:
            errors.append(exc)
    raise errors[-1] if errors else ApiError("no Railway token")


def resolve_ids():
    global PROJECT_ID, ENVIRONMENT_ID, SERVICE_ID
    if not PROJECT_ID or not ENVIRONMENT_ID:
        if not PROJECT_TOKEN:
            raise ApiError("Railway project/environment IDs are unavailable; provide RAILWAY_PROJECT_ID and RAILWAY_ENVIRONMENT_ID when using account-token fallback")
        data = gql("query { projectToken { projectId environmentId } }")
        info = data.get("projectToken") or {}
        PROJECT_ID = PROJECT_ID or str(info.get("projectId", ""))
        ENVIRONMENT_ID = ENVIRONMENT_ID or str(info.get("environmentId", ""))
    if not PROJECT_ID or not ENVIRONMENT_ID:
        raise ApiError("unable to resolve Railway project/environment ID")
    if not SERVICE_ID:
        data = gql("query($id:String!){ project(id:$id){ services{edges{node{id name}}} } }", {"id": PROJECT_ID})
        services = (((data.get("project") or {}).get("services") or {}).get("edges") or [])
        wanted = os.environ.get("RAILWAY_SERVICE_NAME", "").strip()
        matches = [e["node"] for e in services if e.get("node", {}).get("name") == wanted]
        if len(matches) == 1:
            SERVICE_ID = matches[0]["id"]
        elif len(services) == 1:
            SERVICE_ID = services[0]["node"]["id"]
        else:
            raise ApiError("unable to identify target Railway service")


def _normalize_domains(raw):
    result = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            key = str(key).strip()
            if not key:
                continue
            if isinstance(value, dict):
                domain = str(value.get("domain", "")).strip() or (key if key.endswith(".up.railway.app") else "")
                if domain:
                    result.append({"id": key if not key.endswith(".up.railway.app") else "", "domain": domain, "config_key": key})
            elif isinstance(value, str) and value.strip():
                result.append({"id": key, "domain": value.strip(), "config_key": key})
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, dict) and str(value.get("domain", "")).strip():
                result.append({"id": str(value.get("id", "")).strip(), "domain": str(value["domain"]).strip(), "config_key": str(value.get("id", "")).strip()})
    return result


def list_service_domains():
    data = gql("query($id:String!){ environment(id:$id){ config(decryptVariables:false) } }", {"id": ENVIRONMENT_ID})
    config = ((data.get("environment") or {}).get("config")) or {}
    if isinstance(config, str):
        config = json.loads(config)
    service_cfg = ((config.get("services") or {}).get(SERVICE_ID)) or {}
    domains = _normalize_domains((service_cfg.get("networking") or {}).get("serviceDomains") or {})
    print(f"RAILWAY_API_PUBLIC_DOMAIN_CONFIG_COUNT={len(domains)}")
    return domains


def create_service_domain():
    print("RAILWAY_API_ACTION=CREATE_PUBLIC_DOMAIN")
    result = gql("mutation($input:ServiceDomainCreateInput!){ serviceDomainCreate(input:$input){domain} }", {"input": {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID}})
    domain = (result.get("serviceDomainCreate") or {}).get("domain", "")
    if not domain:
        raise ApiError("serviceDomainCreate returned an empty domain")
    print(f"RAILWAY_API_PUBLIC_DOMAIN=CREATED domain={domain}")


def _normalize_tcp(raw):
    out = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        try: app = int(p.get("applicationPort", -1)); port = int(p.get("proxyPort", -1))
        except (TypeError, ValueError): app = port = -1
        out.append({"id": str(p.get("id", "")).strip(), "domain": str(p.get("domain", "")).strip(), "proxyPort": port, "applicationPort": app})
    return out


def ensure_tcp_proxy():
    data = gql("query($serviceId:String!,$environmentId:String!){ tcpProxies(serviceId:$serviceId,environmentId:$environmentId){id domain proxyPort applicationPort} }", {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID})
    proxies = _normalize_tcp(data.get("tcpProxies") or [])
    target = [p for p in proxies if p["applicationPort"] == TARGET_PORT]
    print(f"RAILWAY_API_TCP_PROXY_CONFIG_COUNT={len(proxies)}")
    env_host = os.environ.get("RAILWAY_TCP_PROXY_DOMAIN", "").strip()
    env_port = os.environ.get("RAILWAY_TCP_PROXY_PORT", "").strip()
    if len(target) > 1:
        keep = next((p for p in target if p["domain"] == env_host), target[0])
        print(f"RAILWAY_API_TCP_PROXY=DUPLICATES count={len(target)} keep={keep['domain']}:{keep['proxyPort']}")
        # Do not destructively delete existing proxies during normal startup.
        # Runtime endpoint variables are authoritative; leave extra resources intact.
        return False
    if len(target) == 1:
        p = target[0]
        if not p["domain"] or not 1 <= p["proxyPort"] <= 65535:
            raise ApiError("Railway TCP proxy targeting 8080 has invalid domain or proxy port")
        print(f"RAILWAY_API_TCP_PROXY=EXISTS target=8080 domain={p['domain']} port={p['proxyPort']}")
        if env_host and env_port and (env_host != p["domain"] or env_port != str(p["proxyPort"])):
            print(f"RAILWAY_API_TCP_PROXY_ENV_MISMATCH env={env_host}:{env_port} config={p['domain']}:{p['proxyPort']}")
        return False
    print("RAILWAY_API_ACTION=CREATE_TCP_PROXY target=8080")
    result = gql("mutation($input:TCPProxyCreateInput!){ tcpProxyCreate(input:$input){id domain proxyPort applicationPort} }", {"input": {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID, "applicationPort": TARGET_PORT}})
    p = result.get("tcpProxyCreate") or {}
    if not p.get("domain") or not p.get("proxyPort"):
        raise ApiError("tcpProxyCreate returned incomplete proxy information")
    print(f"RAILWAY_API_TCP_PROXY=CREATED domain={p['domain']} port={p['proxyPort']} target=8080")
    return True


def setup():
    if not PROJECT_TOKEN and not ACCOUNT_TOKEN:
        print("RAILWAY_API_SETUP=SKIP reason=no_token")
        return 0
    resolve_ids()
    print("RAILWAY_API_SETUP=CHECK")
    domains = list_service_domains()
    current_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    changed = False
    if not domains:
        create_service_domain()
        changed = True
    elif len(domains) == 1:
        print(f"RAILWAY_API_PUBLIC_DOMAIN=EXISTS domain={domains[0]['domain']}")
    else:
        keep = next((d for d in domains if d["domain"] == current_domain), domains[0])
        print(f"RAILWAY_API_PUBLIC_DOMAIN=DUPLICATES count={len(domains)} keep={keep['domain']}")
        # Non-destructive: never delete a live domain merely because another exists.
    if ensure_tcp_proxy():
        changed = True
    if changed:
        print("RAILWAY_API_ACTION=REDEPLOY")
        gql("mutation($serviceId:String!,$environmentId:String!){ serviceInstanceRedeploy(serviceId:$serviceId,environmentId:$environmentId) }", {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID})
        print("RAILWAY_API_SETUP=REDEPLOY_REQUESTED")
        return 10
    print("RAILWAY_API_SETUP=READY")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(setup())
    except Exception as exc:
        # Control-plane API is optional when Railway has already injected a
        # complete runtime endpoint set. This is the critical account-change
        # safety path: never take a healthy runtime offline solely because the
        # old/new account token cannot reach the control plane.
        if runtime_endpoints_complete():
            print(f"RAILWAY_API_SETUP=DEGRADED reason={exc}", file=sys.stderr)
            print("RAILWAY_API_SETUP=CONTINUE_RUNTIME_ENDPOINTS", file=sys.stderr)
            sys.exit(0)
        print(f"RAILWAY_API_SETUP=ERROR {exc}", file=sys.stderr)
        sys.exit(20)
