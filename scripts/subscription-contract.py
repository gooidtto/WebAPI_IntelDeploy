#!/usr/bin/env python3
"""Validate the immutable subscription token and runtime endpoint contract.

The token is an identity primitive on the persistent volume. Railway endpoints
are runtime state and may change between deployments. This contract verifies
that the token remains sealed to the persistent identity while the served
subscription and subscription URL exactly follow the current deployment
endpoints, without printing any UUID, key, short ID, or token.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

D = Path(os.environ.get("DATA_DIR", "/data"))
TOKEN_FILE = D / "subscription_token.txt"
SEAL_FILE = D / "identity-integrity.json"
SUB_FILE = D / "subscription.txt"
SUB_URL_FILE = D / "subscription_url.txt"
RUNTIME_FILE = D / "runtime.json"

# Gateway startup and HTTP route readiness are not atomic. Keep the contract
# fail-closed, but allow a short bounded readiness window before failing.
HTTP_ATTEMPTS = max(1, int(os.environ.get("SUBSCRIPTION_HTTP_ATTEMPTS", "15")))
HTTP_TIMEOUT = max(1.0, float(os.environ.get("SUBSCRIPTION_HTTP_TIMEOUT", "3")))
HTTP_RETRY_DELAY = max(0.1, float(os.environ.get("SUBSCRIPTION_HTTP_RETRY_DELAY", "0.5")))


def fail(reason: str) -> None:
    print(f"SUBSCRIPTION_CONTRACT=FAIL reason={reason}", file=sys.stderr)
    raise SystemExit(1)


def read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_token_sealed(token: str) -> None:
    if not SEAL_FILE.is_file():
        fail("IDENTITY_SEAL_MISSING")
    try:
        seal = json.loads(SEAL_FILE.read_text())
        expected = (seal.get("files") or {}).get(TOKEN_FILE.name, "")
    except Exception:
        fail("IDENTITY_SEAL_INVALID")
    if seal.get("schema") != 1 or not expected:
        fail("IDENTITY_SEAL_INVALID")
    if file_sha256(TOKEN_FILE) != expected:
        fail("TOKEN_SEAL_MISMATCH")


def endpoint_fingerprint(public: str, tcp_host: str, tcp_port: str, nodes: int) -> str:
    raw = f"public={public}\ntcp={tcp_host}:{tcp_port}\nnodes={nodes}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def validate_lines(lines, runtime):
    expected = int((runtime.get("nodes") or {}).get("count", 0) or 0)
    if expected not in (5, 6):
        fail("RUNTIME_NODE_COUNT")
    if len(lines) != expected or any(not x.startswith("vless://") for x in lines):
        fail("SUBSCRIPTION_LINE_COUNT_OR_FORMAT")

    public = str(runtime.get("public_domain") or "").strip()
    tcp = runtime.get("tcp_proxy") or {}
    tcp_host = str(tcp.get("domain") or "").strip()
    tcp_port = str(tcp.get("port") or "").strip()
    if not public or not tcp_host or not tcp_port:
        fail("CURRENT_ENDPOINT_STATE")

    if not re.match(rf"^vless://[^@]+@{re.escape(public)}:443\?", lines[0]):
        fail("NODE1_PUBLIC_ENDPOINT")
    if not re.match(rf"^vless://[^@]+@{re.escape(public)}:443\?", lines[1]):
        fail("NODE2_PUBLIC_ENDPOINT")
    for idx in (2, 3, 4):
        if not re.match(rf"^vless://[^@]+@{re.escape(tcp_host)}:{re.escape(tcp_port)}\?", lines[idx]):
            fail(f"NODE{idx+1}_TCP_ENDPOINT")

    if expected == 6:
        cf = runtime.get("cloudflare") or {}
        cf_host = str(cf.get("public_hostname") or "").strip()
        if not cf_host or not re.match(rf"^vless://[^@]+@{re.escape(cf_host)}:443\?", lines[5]):
            fail("NODE6_CLOUDFLARE_ENDPOINT")

    return expected, public, tcp_host, tcp_port


def validate_subscription_url(token: str, public: str) -> None:
    """The local subscription URL file is a required subscription artifact."""
    if not SUB_URL_FILE.is_file():
        fail("SUBSCRIPTION_URL_FILE_MISSING")
    expected = f"https://{public}/sub/{token}"
    actual = read(SUB_URL_FILE)
    if actual != expected:
        fail("SUBSCRIPTION_URL_FILE_MISMATCH")


def local_subscription_request(url: str):
    """Fetch /sub with bounded readiness retries and safe diagnostics."""
    last_status = None
    last_error = None

    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                url,
                method="GET",
                headers={"Cache-Control": "no-cache"},
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                status = int(response.status)
                if status != 200:
                    last_status = status
                    if status in (404, 408, 425, 429) or status >= 500:
                        if attempt < HTTP_ATTEMPTS:
                            print(
                                f"SUBSCRIPTION_HTTP_LOCAL=RETRY attempt={attempt}/{HTTP_ATTEMPTS} status={status}",
                                file=sys.stderr,
                            )
                            time.sleep(HTTP_RETRY_DELAY)
                            continue
                    fail(f"HTTP_STATUS_{status}")
                return response.read()

        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            last_status = status
            # The gateway can expose 404 briefly while its HTTP route is
            # starting. Retry 404/408/425/429 and all 5xx responses only within
            # the bounded startup window. Permanent 401/403-style failures are
            # not retried and fail closed immediately.
            retryable = status in (404, 408, 425, 429) or status >= 500
            if retryable and attempt < HTTP_ATTEMPTS:
                print(
                    f"SUBSCRIPTION_HTTP_LOCAL=RETRY attempt={attempt}/{HTTP_ATTEMPTS} status={status}",
                    file=sys.stderr,
                )
                time.sleep(HTTP_RETRY_DELAY)
                continue
            fail(f"HTTP_STATUS_{status}")

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = type(exc).__name__
            if attempt < HTTP_ATTEMPTS:
                print(
                    f"SUBSCRIPTION_HTTP_LOCAL=RETRY attempt={attempt}/{HTTP_ATTEMPTS} error={last_error}",
                    file=sys.stderr,
                )
                time.sleep(HTTP_RETRY_DELAY)
                continue
            fail(f"HTTP_LOCAL_ACCESS_{last_error}")

    if last_status is not None:
        fail(f"HTTP_STATUS_{last_status}")
    fail(f"HTTP_LOCAL_ACCESS_{last_error or 'UNKNOWN'}")


def main():
    token = read(TOKEN_FILE)
    if not (20 <= len(token) <= 256) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        fail("TOKEN_INVALID")
    verify_token_sealed(token)
    if not RUNTIME_FILE.is_file() or not SUB_FILE.is_file():
        fail("RUNTIME_OR_SUBSCRIPTION_MISSING")

    try:
        runtime = json.loads(RUNTIME_FILE.read_text())
    except Exception:
        fail("RUNTIME_JSON_INVALID")
    lines = [x.strip() for x in SUB_FILE.read_text().splitlines() if x.strip()]
    expected, public, tcp_host, tcp_port = validate_lines(lines, runtime)
    validate_subscription_url(token, public)

    # Contract version is derived only from non-secret subscription semantics.
    contract_material = {
        "schema": 1,
        "nodes": expected,
        "public_domain": public,
        "tcp_proxy": f"{tcp_host}:{tcp_port}",
        "cloudflare": bool((runtime.get("cloudflare") or {}).get("enabled")),
    }
    version = hashlib.sha256(
        json.dumps(contract_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    epfp = endpoint_fingerprint(public, tcp_host, tcp_port, expected)

    # Verify the gateway's subscription URL locally. The token itself is never logged.
    gateway_port = int(os.environ.get("GATEWAY_PORT", "8080"))
    url = f"http://127.0.0.1:{gateway_port}/sub/{token}"
    raw = local_subscription_request(url)

    try:
        decoded = base64.b64decode(raw, validate=True).decode()
    except Exception:
        fail("HTTP_PAYLOAD_NOT_BASE64")
    served = [x.strip() for x in decoded.splitlines() if x.strip()]
    if served != lines:
        fail("HTTP_PAYLOAD_MISMATCH")
    validate_lines(served, runtime)

    print("SUBSCRIPTION_TOKEN_STATE=REUSED")
    print("SUBSCRIPTION_TOKEN_SEALED=PASS")
    print("SUBSCRIPTION_TOKEN_SECRET=REDACTED")
    print("SUBSCRIPTION_URL_FILE=PASS")
    print("SUBSCRIPTION_HTTP_LOCAL=PASS")
    print("SUBSCRIPTION_ENDPOINT_CONTRACT=PASS")
    print(f"SUBSCRIPTION_VERSION={version}")
    print(f"SUBSCRIPTION_ENDPOINT_FINGERPRINT={epfp}")
    print(f"SUBSCRIPTION_ENDPOINT_STATE=public={public} tcp={tcp_host}:{tcp_port} nodes={expected}")
    print("SUBSCRIPTION_SECRETS_EXPOSED=NO")
    print("SUBSCRIPTION_CONTRACT=PASS")


if __name__ == "__main__":
    main()
