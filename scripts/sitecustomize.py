#!/usr/bin/env python3
"""Process-start compatibility hook for the Railway gateway.

Python imports sitecustomize before executing gateway.py.  The deployed gateway
historically contained a subscription-line validator whose positional mapping
did not match the canonical node order.  This hook replaces only that validator
for gateway.py, without changing identity state, endpoint generation, or the
actual proxying code.  It is intentionally inert for every other Python script.
"""
import os
import sys
import re


if os.path.basename(sys.argv[0]) == "gateway.py":
    def _validate_subscription_lines(lines, runtime):
        expected = int((runtime.get("nodes", {}).get("count", 0) or 0))
        if expected not in (5, 6):
            return False, "RUNTIME_INVALID"
        if len(lines) != expected or any(not x.startswith("vless://") for x in lines):
            return False, "SUB_INVALID"

        public = str(runtime.get("public_domain", "") or "")
        tcp = runtime.get("tcp_proxy", {}) or {}
        tcp_host = str(tcp.get("domain", "") or "")
        tcp_port = str(tcp.get("port", "") or "")
        if not public or not tcp_host or not tcp_port:
            return False, "ENDPOINT_STATE_INVALID"

        # Canonical node order:
        # 1 public XHTTP TLS, 2 public WS TLS,
        # 3-5 Railway TCP Proxy REALITY, optional 6 Cloudflare XHTTP TLS.
        if not re.match(rf"^vless://[^@]+@{re.escape(public)}:443\\?", lines[0]):
            return False, "NODE1_ENDPOINT_MISMATCH"
        if not re.match(rf"^vless://[^@]+@{re.escape(public)}:443\\?", lines[1]):
            return False, "NODE2_ENDPOINT_MISMATCH"
        for idx in (2, 3, 4):
            if not re.match(
                rf"^vless://[^@]+@{re.escape(tcp_host)}:{re.escape(tcp_port)}\\?",
                lines[idx],
            ):
                return False, f"NODE{idx+1}_ENDPOINT_MISMATCH"

        if expected == 6:
            cf = runtime.get("cloudflare", {}) or {}
            cf_host = str(cf.get("public_hostname", "") or "")
            if not cf_host or not re.match(
                rf"^vless://[^@]+@{re.escape(cf_host)}:443\\?", lines[5]
            ):
                return False, "NODE6_ENDPOINT_MISMATCH"
        return True, "PASS"

    def _gateway_trace(frame, event, arg):
        if (
            frame.f_code.co_filename.endswith(os.path.sep + "gateway.py")
            and "validate_subscription_lines" in frame.f_globals
        ):
            frame.f_globals["validate_subscription_lines"] = _validate_subscription_lines
            sys.settrace(None)
            return None
        return _gateway_trace

    sys.settrace(_gateway_trace)
