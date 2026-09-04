#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
import re
import socket
import struct
import urllib.parse
from pathlib import Path

PORT = int(os.environ.get("GATEWAY_PORT", "8080"))
D = Path(os.environ.get("DATA_DIR", "/data"))
SITE = Path("/opt/xray/site/index.html")
TOKEN = D / "subscription_token.txt"
SUB = D / "subscription.txt"
RUNTIME = D / "runtime.json"
HTTP_DEST = ("127.0.0.1", 10086)
WS_DEST = ("127.0.0.1", 10090)
XPATH = os.environ.get("XHTTP_PATH", "/xhttp").strip() or "/xhttp"
WSPATH = os.environ.get("WS_PATH", "/ws").strip() or "/ws"
RAW_SNI = os.environ.get("REALITY_RAW_SNI", "www.cloudflare.com").strip().lower().rstrip(".") or "www.cloudflare.com"
XHTTP_SNI = os.environ.get("REALITY_XHTTP_SNI", "www.apple.com").strip().lower().rstrip(".") or "www.apple.com"
GRPC_SNI = os.environ.get("REALITY_GRPC_SNI", "www.bing.com").strip().lower().rstrip(".") or "www.bing.com"
ROUTES = {
    RAW_SNI: ("127.0.0.1", 10087, "raw-reality-vision"),
    XHTTP_SNI: ("127.0.0.1", 10088, "xhttp-reality"),
    GRPC_SNI: ("127.0.0.1", 10089, "grpc-reality"),
}
MAX_CONNECTIONS = max(16, int(os.environ.get("GATEWAY_MAX_CONNECTIONS", "512")))
INITIAL_TIMEOUT = max(2.0, float(os.environ.get("GATEWAY_READ_TIMEOUT", "20")))
UPSTREAM_TIMEOUT = max(2.0, float(os.environ.get("GATEWAY_UPSTREAM_TIMEOUT", "10")))
IDLE_TIMEOUT = max(30.0, float(os.environ.get("GATEWAY_IDLE_TIMEOUT", "900")))
MAX_INITIAL = min(262144, max(4096, int(os.environ.get("GATEWAY_MAX_INITIAL", "131072"))))
SEM = asyncio.Semaphore(MAX_CONNECTIONS)
HTTP = (b"GET ", b"POST ", b"HEAD ", b"PUT ", b"OPTIONS ", b"PATCH ", b"DELETE ", b"PRI * HTTP/2.0")
logging.basicConfig(level=getattr(logging, os.environ.get("GATEWAY_LOGLEVEL", "INFO").upper(), logging.INFO), format="[gateway] %(levelname)s %(message)s")
log = logging.getLogger("gateway")


def load_runtime():
    try:
        return json.loads(RUNTIME.read_text())
    except Exception:
        return {}


def expected_nodes():
    n = int(load_runtime().get("nodes", {}).get("count", 0) or 0)
    return n if n in (5, 6) else 0


def local_port_ready(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def cloudflare_ready():
    try:
        cf = load_runtime().get("cloudflare", {})
        if not cf.get("enabled"):
            return True
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:2000/ready", timeout=2).read()
        return True
    except Exception:
        return False


def validate_subscription_lines(lines, runtime):
    expected = int(runtime.get("nodes", {}).get("count", 0) or 0)
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

    # Canonical node order must exactly match generate.py:
    # 1 public XHTTP TLS, 2 public WS TLS,
    # 3-5 Railway TCP Proxy REALITY, optional 6 Cloudflare XHTTP TLS.
    if not re.match(rf"^vless://[^@]+@{re.escape(public)}:443\?", lines[0]):
        return False, "NODE1_ENDPOINT_MISMATCH"
    if not re.match(rf"^vless://[^@]+@{re.escape(public)}:443\?", lines[1]):
        return False, "NODE2_ENDPOINT_MISMATCH"
    for idx in (2, 3, 4):
        if not re.match(rf"^vless://[^@]+@{re.escape(tcp_host)}:{re.escape(tcp_port)}\?", lines[idx]):
            return False, f"NODE{idx+1}_ENDPOINT_MISMATCH"

    if expected == 6:
        cf = runtime.get("cloudflare", {}) or {}
        cf_host = str(cf.get("public_hostname", "") or "")
        if not cf_host or not re.match(rf"^vless://[^@]+@{re.escape(cf_host)}:443\?", lines[5]):
            return False, "NODE6_ENDPOINT_MISMATCH"
    return True, "PASS"


def readiness():
    runtime = load_runtime()
    expected = int(runtime.get("nodes", {}).get("count", 0) or 0)
    if expected not in (5, 6):
        return False, "runtime"
    if not RUNTIME.exists() or not SUB.exists() or not TOKEN.exists():
        return False, "state"
    lines = [x.strip() for x in SUB.read_text().splitlines() if x.strip()]
    ok, reason = validate_subscription_lines(lines, runtime)
    if not ok:
        return False, "subscription-" + reason
    for port, label in ((10086, "xhttp-http"), (10090, "ws-http"), (10087, "raw-reality"), (10088, "xhttp-reality"), (10089, "grpc-reality")):
        if not local_port_ready(port):
            return False, label
    if not cloudflare_ready():
        return False, "cloudflare"
    return True, "ready"


def current_runtime_subscription():
    runtime = load_runtime()
    expected = int(runtime.get("nodes", {}).get("count", 0) or 0)
    if expected not in (5, 6):
        return None, "RUNTIME_INVALID"
    if not SUB.exists():
        return None, "SUB_MISSING"
    # Read the freshly generated file, then validate it against the current runtime
    # on every HTTP fetch. This prevents stale persistent-volume endpoints from being
    # returned after Railway networking changes.
    lines = [x.strip() for x in SUB.read_text().splitlines() if x.strip()]
    ok, reason = validate_subscription_lines(lines, runtime)
    if not ok:
        return None, reason
    return lines, "PASS"


def subscription(token):
    if not TOKEN.exists() or token != TOKEN.read_text().strip():
        return None, "TOKEN_INVALID"
    lines, status = current_runtime_subscription()
    if lines is None:
        log.error("SUBSCRIPTION_HTTP_RENDER=FAIL reason=%s", status)
        return None, status
    payload = base64.b64encode(("\n".join(lines) + "\n").encode())
    runtime = load_runtime()
    log.warning(
        "SUBSCRIPTION_HTTP_RENDER=PASS public=%s tcp=%s:%s nodes=%s",
        runtime.get("public_domain", ""),
        (runtime.get("tcp_proxy", {}) or {}).get("domain", ""),
        (runtime.get("tcp_proxy", {}) or {}).get("port", ""),
        runtime.get("nodes", {}).get("count", 0),
    )
    return payload, "PASS"


def _parse_client_hello_sni(handshake):
    try:
        if len(handshake) < 4 or handshake[0] != 1: return None
        hs_len = int.from_bytes(handshake[1:4], "big")
        if hs_len < 34 or 4 + hs_len > len(handshake): return None
        end = 4 + hs_len; p = 4 + 34
        if p + 1 > end: return None
        p += 1 + handshake[p]
        if p + 2 > end: return None
        cipher_len = struct.unpack("!H", handshake[p:p+2])[0]; p += 2 + cipher_len
        if p + 1 > end: return None
        p += 1 + handshake[p]
        if p + 2 > end: return None
        ext_len = struct.unpack("!H", handshake[p:p+2])[0]; p += 2
        if p + ext_len > end: return None
        ext_end = p + ext_len
        while p + 4 <= ext_end:
            typ, ln = struct.unpack("!HH", handshake[p:p+4]); p += 4
            if p + ln > ext_end: return None
            if typ == 0 and ln >= 5:
                q = p + 2; stop = p + ln
                while q + 3 <= stop:
                    name_type = handshake[q]; name_len = struct.unpack("!H", handshake[q+1:q+3])[0]; q += 3
                    if q + name_len > stop: return None
                    if name_type == 0: return handshake[q:q+name_len].decode("idna").strip().lower().rstrip(".")
                    q += name_len
            p += ln
    except (IndexError, struct.error, UnicodeError): return None
    return None


def _tls_client_hello(buf):
    if len(buf) < 5 or buf[0] != 0x16 or buf[1] != 0x03: return False, None
    pos = 0; handshake = bytearray()
    while pos + 5 <= len(buf):
        typ, major, minor, ln = buf[pos], buf[pos+1], buf[pos+2], struct.unpack("!H", buf[pos+3:pos+5])[0]
        if major != 3 or minor not in (0,1,2,3,4) or typ not in (20,21,22,23): return False, None
        if pos + 5 + ln > len(buf): break
        payload = buf[pos+5:pos+5+ln]
        if typ == 22:
            handshake.extend(payload)
            while len(handshake) >= 4:
                if handshake[0] != 1: return False, None
                hs_len = int.from_bytes(handshake[1:4], "big"); total = 4 + hs_len
                if len(handshake) < total: break
                sni = _parse_client_hello_sni(bytes(handshake[:total]))
                if sni: return True, sni
                del handshake[:total]
        pos += 5 + ln
    return False, None


def tls_sni(buf):
    complete, sni = _tls_client_hello(buf)
    if sni: return sni
    low = bytes(buf).lower()
    for candidate in ROUTES:
        if candidate.encode("ascii") in low: return candidate
    return None


async def read_initial(reader):
    buf = bytearray(); deadline = asyncio.get_running_loop().time() + INITIAL_TIMEOUT
    while len(buf) < MAX_INITIAL:
        left = max(0.05, deadline - asyncio.get_running_loop().time())
        try: chunk = await asyncio.wait_for(reader.read(min(8192, MAX_INITIAL-len(buf))), left)
        except asyncio.TimeoutError: break
        if not chunk: break
        buf.extend(chunk); b = bytes(buf)
        if b.startswith(HTTP):
            if b"\r\n\r\n" in b or len(b) > 8192: return b
        elif len(b) >= 3 and b[0] == 0x16 and b[1] == 0x03:
            complete, sni = _tls_client_hello(b)
            if complete or sni: return b
            early_sni = tls_sni(b)
            if early_sni:
                log.info("TLS_SNI_EARLY sni=%s initial=%d", early_sni, len(b)); return b
        elif b[:1] != b"\x16": return b
    return bytes(buf)


async def pipe(r, w, direction):
    try:
        while True:
            b = await asyncio.wait_for(r.read(65536), timeout=IDLE_TIMEOUT)
            if not b: return
            w.write(b); await w.drain()
    except asyncio.CancelledError: raise
    except Exception as exc: log.warning("RELAY_ERROR direction=%s error=%s:%s", direction, type(exc).__name__, exc)


async def relay(reader, writer, initial, dest, label, sni="-"):
    up = None; tasks = set()
    try:
        log.info("ROUTE_SELECTED route=%s sni=%s dest=%s:%s initial=%d", label, sni, dest[0], dest[1], len(initial))
        ur, up = await asyncio.wait_for(asyncio.open_connection(*dest), timeout=UPSTREAM_TIMEOUT)
        log.info("UPSTREAM_CONNECT_OK route=%s dest=%s:%s", label, dest[0], dest[1])
        if initial:
            up.write(initial); await up.drain(); log.info("INITIAL_FORWARDED route=%s bytes=%d", label, len(initial))
        tasks = {asyncio.create_task(pipe(reader,up,"client->upstream")), asyncio.create_task(pipe(ur,writer,"upstream->client"))}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try: task.result()
            except Exception: pass
        for task in pending: task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.TimeoutError: log.warning("UPSTREAM_TIMEOUT route=%s dest=%s:%s", label, dest[0], dest[1])
    except Exception as exc: log.warning("RELAY_ERROR route=%s dest=%s:%s error=%s:%s", label, dest[0], dest[1], type(exc).__name__, exc)
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        for sock in (writer,up):
            if sock:
                try: sock.close(); await sock.wait_closed()
                except Exception: pass


async def write_response(writer,status,body=b"",content_type=b"text/plain; charset=utf-8"):
    writer.write(b"HTTP/1.1 "+status+b"\r\nContent-Type: "+content_type+b"\r\nContent-Length: "+str(len(body)).encode()+b"\r\nCache-Control: no-store, no-cache, must-revalidate, max-age=0\r\nPragma: no-cache\r\nExpires: 0\r\nConnection: close\r\n\r\n"+body); await writer.drain()


async def http(reader,writer,initial):
    first=initial.split(b"\r\n",1)[0].decode("latin1","ignore"); parts=first.split(" ",2); method=parts[0] if parts else ""; target=parts[1] if len(parts)>1 else ""; path=urllib.parse.urlsplit(target).path
    if method in ("GET","HEAD") and path in ("/health","/ready"):
        runtime=load_runtime(); cf=runtime.get("cloudflare",{}) or {}
        ok,reason=readiness() if path=="/ready" else (True,"healthy")
        body=(json.dumps({"status":"ready" if ok else "not-ready","reason":None if ok else reason,"nodes":int(runtime.get("nodes",{}).get("count",0) or 0),"subscription":len([x for x in SUB.read_text().splitlines() if x.strip()]) if SUB.exists() else 0,"cloudflare":bool(cf.get("enabled")),"transport":"xhttp" if cf.get("enabled") else None},separators=(",",":"))+"\n").encode()
        if method=="HEAD": body=b""
        await write_response(writer,b"200 OK" if ok else b"503 Service Unavailable",body,b"application/json; charset=utf-8"); return
    m=re.fullmatch(r"/sub/([A-Za-z0-9_-]{20,128})/?",path)
    if method in ("GET","HEAD") and m:
        payload,status=subscription(urllib.parse.unquote(m.group(1)))
        if payload is not None: await write_response(writer,b"200 OK",b"" if method=="HEAD" else payload,b"text/plain; charset=utf-8")
        else: await write_response(writer,b"404 Not Found" if status=="TOKEN_INVALID" else b"500 Internal Server Error",(status+"\n").encode())
        return
    if method in ("GET","HEAD") and path in ("/","/index.html"):
        body=SITE.read_bytes(); await write_response(writer,b"200 OK",b"" if method=="HEAD" else body,b"text/html; charset=utf-8"); return
    if path == XPATH:
        await relay(reader,writer,initial,HTTP_DEST,"http-xhttp","-"); return
    if path == WSPATH:
        await relay(reader,writer,initial,WS_DEST,"http-websocket","-"); return
    await relay(reader,writer,initial,HTTP_DEST,"http-xhttp","-")


async def handle(reader,writer):
    peer=writer.get_extra_info("peername"); log.info("TCP_ACCEPT peer=%s local=%s",peer,writer.get_extra_info("sockname"))
    async with SEM:
        try:
            initial=await read_initial(reader)
            if not initial: return
            log.info("INITIAL_RECEIVED peer=%s bytes=%d first=0x%s",peer,len(initial),initial[:1].hex() if initial else "-")
            if initial.startswith(HTTP): log.info("PROTOCOL_DETECTED peer=%s protocol=http",peer); await http(reader,writer,initial); return
            if initial[:1]==b"\x16" and len(initial)>=3 and initial[1]==0x03:
                sni=tls_sni(initial); log.info("TLS_SNI peer=%s sni=%s initial=%d",peer,sni or "-",len(initial)); route=ROUTES.get(sni or "")
                if route: log.info("ROUTE_MATCH peer=%s sni=%s route=%s dest=%s:%s",peer,sni,route[2],route[0],route[1]); await relay(reader,writer,initial,(route[0],route[1]),route[2],sni); return
                log.warning("ROUTE_REJECT tls_sni=%s peer=%s initial=%d",sni or "-",peer,len(initial)); return
            log.warning("ROUTE_REJECT unknown_protocol=0x%s peer=%s",initial[:1].hex() if initial else "-",peer)
        except Exception as exc: log.warning("ERROR peer=%s error=%s:%s",peer,type(exc).__name__,exc)
        finally:
            try: writer.close(); await writer.wait_closed()
            except Exception: pass


async def main():
    server=await asyncio.start_server(handle,"0.0.0.0",PORT,limit=262144)
    log.info("GATEWAY_READY=%s max_connections=%s idle_timeout=%ss",PORT,MAX_CONNECTIONS,IDLE_TIMEOUT)
    log.info("ROUTES=%s", ",".join(f"{k}->{v[1]}" for k,v in ROUTES.items()))
    await server.serve_forever()

if __name__=="__main__": asyncio.run(main())
