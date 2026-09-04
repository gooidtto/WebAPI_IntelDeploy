#!/usr/bin/env python3
"""Idempotent Railway networking bootstrap.

This module owns runtime networking only:
  Railway service domain -> HTTP/TLS gateway on :8080
  Railway TCP proxy -> TCP gateway on :8080

It never creates, rotates, repairs, or otherwise changes node identity.
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
TOKEN = PROJECT_TOKEN or ACCOUNT_TOKEN
PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "").strip()
AUTH_MODE = None


class ApiError(RuntimeError):
    pass


def _request_once(query, variables, mode):
    headers = {"Content-Type": "application/json", "User-Agent": "railway-universal-stable/5.6"}
    headers["Project-Access-Token" if mode == "project" else "Authorization"] = (
        TOKEN if mode == "project" else f"Bearer {TOKEN}"
    )
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        err = ApiError(f"HTTP {exc.code}: {detail[:500]}")
        if exc.code == 429 or 500 <= exc.code <= 599:
            err.retryable = True
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


def _request(query, variables, mode):
    last = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            return _request_once(query, variables, mode)
        except ApiError as exc:
            last = exc
            if not getattr(exc, "retryable", False) or attempt >= API_RETRIES:
                raise
            print(f"RAILWAY_API_RETRY={attempt}/{API_RETRIES} delay={API_RETRY_DELAY:g}s reason=transient")
            time.sleep(API_RETRY_DELAY)
    raise last or ApiError("Railway API request failed")


def gql(query, variables=None):
    global AUTH_MODE
    if not TOKEN:
        raise ApiError("no Railway token")
    if AUTH_MODE:
        return _request(query, variables or {}, AUTH_MODE)
    if PROJECT_TOKEN:
        try:
            data = _request(query, variables or {}, "project")
            AUTH_MODE = "project"
            return data
        except ApiError as project_error:
            try:
                data = _request(query, variables or {}, "bearer")
                AUTH_MODE = "bearer"
                print("RAILWAY_API_AUTH=BEARER_FALLBACK")
                return data
            except ApiError:
                raise project_error
    AUTH_MODE = "bearer"
    return _request(query, variables or {}, "bearer")


def resolve_ids():
    global PROJECT_ID, ENVIRONMENT_ID, SERVICE_ID
    if not PROJECT_ID or not ENVIRONMENT_ID:
        if not PROJECT_TOKEN:
            raise ApiError("Railway project/environment IDs are unavailable")
        data = gql("query { projectToken { projectId environmentId } }")
        info = data.get("projectToken") or {}
        PROJECT_ID = PROJECT_ID or str(info.get("projectId", ""))
        ENVIRONMENT_ID = ENVIRONMENT_ID or str(info.get("environmentId", ""))
    if not PROJECT_ID or not ENVIRONMENT_ID:
        raise ApiError("unable to resolve Railway project/environment ID")
    if not SERVICE_ID:
        data = gql(
            """query($id:String!){ project(id:$id){ services{edges{node{id name}}} } }""",
            {"id": PROJECT_ID},
        )
        services = (((data.get("project") or {}).get("services") or {}).get("edges") or [])
        wanted = os.environ.get("RAILWAY_SERVICE_NAME", "").strip()
        matches = [e["node"] for e in services if e.get("node", {}).get("name") == wanted]
        if len(matches) == 1:
            SERVICE_ID = matches[0]["id"]
        elif len(services) == 1:
            SERVICE_ID = services[0]["node"]["id"]
        else:
            raise ApiError("unable to identify target Railway service")


def _normalize_domain_entries(raw):
    """Normalize Railway environment-config serviceDomains without assuming a schema field."""
    result = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            key = str(key).strip()
            if not key:
                continue
            if isinstance(value, dict):
                domain = str(value.get("domain", "")).strip()
                if not domain and key.endswith(".up.railway.app"):
                    domain = key
                if domain:
                    result.append({"id": key if not key.endswith(".up.railway.app") else "", "domain": domain, "config_key": key})
            elif isinstance(value, str) and value.strip():
                result.append({"id": key, "domain": value.strip(), "config_key": key})
    elif isinstance(raw, list):
        for value in raw:
            if not isinstance(value, dict):
                continue
            domain = str(value.get("domain", "")).strip()
            domain_id = str(value.get("id", "")).strip()
            if domain:
                result.append({"id": domain_id, "domain": domain, "config_key": domain_id})
    return result


def list_service_domains():
    """Read Railway-provided domains from the environment configuration."""
    data = gql(
        """query($id:String!){ environment(id:$id){ config(decryptVariables:false) } }""",
        {"id": ENVIRONMENT_ID},
    )
    config = ((data.get("environment") or {}).get("config")) or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception as exc:
            raise ApiError(f"unable to parse Railway environment config: {exc}")
    service_cfg = ((config.get("services") or {}).get(SERVICE_ID)) or {}
    networking = service_cfg.get("networking") or {}
    domains = _normalize_domain_entries(networking.get("serviceDomains") or {})
    print(f"RAILWAY_API_PUBLIC_DOMAIN_CONFIG_COUNT={len(domains)}")
    if domains:
        print("RAILWAY_API_PUBLIC_DOMAIN_CONFIG=" + ",".join(d["domain"] for d in domains))
    return domains


def delete_service_domain(domain_id, domain, config_key=""):
    print(f"RAILWAY_API_ACTION=DELETE_DUPLICATE_PUBLIC_DOMAIN domain={domain}")
    if domain_id:
        gql("""mutation($id:String!){ serviceDomainDelete(id:$id) }""", {"id": domain_id})
    elif config_key:
        gql(
            """mutation($environmentId:String!,$patch:EnvironmentConfig,$commitMessage:String){
                 environmentPatchCommit(environmentId:$environmentId,patch:$patch,commitMessage:$commitMessage)
               }""",
            {
                "environmentId": ENVIRONMENT_ID,
                "patch": {"services": {SERVICE_ID: {"networking": {"serviceDomains": {config_key: None}}}}},
                "commitMessage": "Remove duplicate Railway service domain",
            },
        )
    else:
        raise ApiError(f"duplicate Railway domain has no deletion key: {domain}")
    print(f"RAILWAY_API_PUBLIC_DOMAIN=DELETED domain={domain}")


def create_service_domain():
    print("RAILWAY_API_ACTION=CREATE_PUBLIC_DOMAIN")
    result = gql(
        """mutation($input:ServiceDomainCreateInput!){ serviceDomainCreate(input:$input){domain} }""",
        {"input": {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID}},
    )
    domain = (result.get("serviceDomainCreate") or {}).get("domain", "")
    if not domain:
        raise ApiError("serviceDomainCreate returned an empty domain")
    print(f"RAILWAY_API_PUBLIC_DOMAIN=CREATED domain={domain}")


def _normalize_tcp_proxies(raw):
    result = []
    for proxy in raw or []:
        if not isinstance(proxy, dict):
            continue
        try:
            application_port = int(proxy.get("applicationPort", -1))
            proxy_port = int(proxy.get("proxyPort", -1))
        except (TypeError, ValueError):
            application_port = -1
            proxy_port = -1
        result.append({
            "id": str(proxy.get("id", "")).strip(),
            "domain": str(proxy.get("domain", "")).strip(),
            "proxyPort": proxy_port,
            "applicationPort": application_port,
        })
    return result


def delete_tcp_proxy(proxy):
    proxy_id = proxy.get("id", "")
    if not proxy_id:
        raise ApiError("Railway TCP proxy has no deletion id")
    print(f"RAILWAY_API_ACTION=DELETE_DUPLICATE_TCP_PROXY domain={proxy.get('domain','')} port={proxy.get('proxyPort','')}")
    gql("""mutation($id:String!){ tcpProxyDelete(id:$id) }""", {"id": proxy_id})
    print(f"RAILWAY_API_TCP_PROXY=DELETED id={proxy_id}")


def ensure_tcp_proxy():
    raw = gql(
        """query($serviceId:String!,$environmentId:String!){
             tcpProxies(serviceId:$serviceId,environmentId:$environmentId){id domain proxyPort applicationPort}
           }""",
        {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID},
    ).get("tcpProxies") or []
    normalized = _normalize_tcp_proxies(raw)
    target = [p for p in normalized if p["applicationPort"] == TARGET_PORT]
    print(f"RAILWAY_API_TCP_PROXY_CONFIG_COUNT={len(normalized)}")
    if normalized:
        print("RAILWAY_API_TCP_PROXY_CONFIG=" + ",".join(
            f"{p['domain']}:{p['proxyPort']}->{p['applicationPort']}" for p in normalized
        ))

    if len(target) > 1:
        keep = next((p for p in target if p["domain"] == os.environ.get("RAILWAY_TCP_PROXY_DOMAIN", "").strip()), target[0])
        print(f"RAILWAY_API_TCP_PROXY=DUPLICATES count={len(target)} keep={keep['domain']}:{keep['proxyPort']}")
        for proxy in target:
            if proxy is keep:
                continue
            delete_tcp_proxy(proxy)
        print("RAILWAY_API_TCP_PROXY=RECONCILED target=8080 count=1")
        return True

    if len(target) == 1:
        proxy = target[0]
        if not proxy["domain"] or not (1 <= proxy["proxyPort"] <= 65535):
            raise ApiError("Railway TCP proxy targeting 8080 has invalid domain or proxy port")
        print(f"RAILWAY_API_TCP_PROXY=EXISTS target=8080 domain={proxy['domain']} port={proxy['proxyPort']}")
        env_host = os.environ.get("RAILWAY_TCP_PROXY_DOMAIN", "").strip()
        env_port = os.environ.get("RAILWAY_TCP_PROXY_PORT", "").strip()
        if env_host and env_port and (env_host != proxy["domain"] or env_port != str(proxy["proxyPort"])):
            print(f"RAILWAY_API_TCP_PROXY_ENV_MISMATCH env={env_host}:{env_port} config={proxy['domain']}:{proxy['proxyPort']}")
        return False

    print("RAILWAY_API_ACTION=CREATE_TCP_PROXY target=8080")
    result = gql(
        """mutation($input:TCPProxyCreateInput!){ tcpProxyCreate(input:$input){id domain proxyPort applicationPort} }""",
        {"input": {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID, "applicationPort": TARGET_PORT}},
    )
    proxy = result.get("tcpProxyCreate") or {}
    if not proxy.get("domain") or not proxy.get("proxyPort"):
        raise ApiError("tcpProxyCreate returned incomplete proxy information")
    print(f"RAILWAY_API_TCP_PROXY=CREATED domain={proxy.get('domain','')} port={proxy.get('proxyPort','')} target=8080")
    return True


def setup():
    if not TOKEN:
        print("RAILWAY_API_SETUP=SKIP reason=no_token")
        return 0
    resolve_ids()
    print("RAILWAY_API_SETUP=CHECK")
    service_domains = list_service_domains()
    current_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    changed = False
    patch_deploy_happened = False

    if not service_domains:
        create_service_domain()
        changed = True
    elif len(service_domains) == 1:
        configured = service_domains[0]["domain"]
        print(f"RAILWAY_API_PUBLIC_DOMAIN=EXISTS domain={configured}")
        if current_domain and current_domain != configured:
            print(f"RAILWAY_API_PUBLIC_DOMAIN_ENV_MISMATCH env={current_domain} config={configured}")
    else:
        keep = next((d for d in service_domains if d["domain"] == current_domain), service_domains[0])
        print(f"RAILWAY_API_PUBLIC_DOMAIN=DUPLICATES count={len(service_domains)} keep={keep['domain']}")
        for domain in service_domains:
            if domain is keep:
                continue
            delete_service_domain(domain["id"], domain["domain"], domain.get("config_key", ""))
            changed = True
            if not domain["id"]:
                patch_deploy_happened = True
        print(f"RAILWAY_API_PUBLIC_DOMAIN=RECONCILED count=1 keep={keep['domain']}")

    if ensure_tcp_proxy():
        changed = True

    if changed and not patch_deploy_happened:
        print("RAILWAY_API_ACTION=REDEPLOY")
        gql(
            """mutation($serviceId:String!,$environmentId:String!){
                 serviceInstanceRedeploy(serviceId:$serviceId,environmentId:$environmentId)
               }""",
            {"serviceId": SERVICE_ID, "environmentId": ENVIRONMENT_ID},
        )
        print("RAILWAY_API_SETUP=REDEPLOY_REQUESTED")
        return 10
    if changed:
        print("RAILWAY_API_SETUP=REDEPLOY_REQUESTED")
        return 10
    print("RAILWAY_API_SETUP=READY")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(setup())
    except Exception as exc:
        print(f"RAILWAY_API_SETUP=ERROR {exc}", file=sys.stderr)
        sys.exit(20)
