#!/usr/bin/env python3
"""Initialize and persist node identity on the mounted persistent volume.

Identity is a persistent deployment primitive. A brand-new empty volume may be
initialized once. Once the marker exists, the complete identity must remain
present and match its integrity seal; otherwise startup fails closed and no
identity is regenerated.

The subscription token is immutable during normal operation. An intentional,
operator-controlled token rotation is allowed only through the
SUBSCRIPTION_TOKEN_ROTATE_ID environment variable. The rotation ID is a
one-time request key; repeating the same ID is idempotent and never rotates a
second time.
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

D = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.environ.get("DATA_DIR", "/data")))

UUID_FILE = D / "uuid.txt"
PRIV_FILE = D / "reality_private_key.txt"
PUB_FILE = D / "reality_public_key.txt"
TOKEN_FILE = D / "subscription_token.txt"
IDS_FILE = D / "reality_short_ids.json"
SEAL_FILE = D / "identity-integrity.json"
MARKER = D / ".node-identity-initialized"
ROTATION_STATE_FILE = D / "subscription-token-rotation.json"

IDENTITY_FILES = (UUID_FILE, PRIV_FILE, PUB_FILE, TOKEN_FILE, IDS_FILE)
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
REALITY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
SHORT_ID_RE = re.compile(r"^[0-9a-fA-F]{2,32}$")
ROTATION_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{3}$")


def persistent_mount_present(path: Path) -> bool:
    """Require the identity directory to be an actual mount point."""
    try:
        target = str(path.resolve())
        if target == "/":
            return False
        if not os.path.ismount(target):
            return False
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) >= 6 and fields[4] == target:
                    return True
    except (OSError, ValueError):
        return False
    return False


def require_persistent_mount() -> None:
    if not persistent_mount_present(D):
        raise SystemExit(
            f"FATAL: {D} is not a mounted persistent volume; "
            "refusing to initialize or run node identity"
        )


def atomic_write(path: Path, value: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        tmp.write_text(value.strip() + "\n")
        os.chmod(tmp, 0o600)
        with tmp.open("r+") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def read_nonempty(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def identity_complete() -> bool:
    uuid = read_nonempty(UUID_FILE)
    private = read_nonempty(PRIV_FILE)
    public = read_nonempty(PUB_FILE)
    token = read_nonempty(TOKEN_FILE)
    ids_raw = read_nonempty(IDS_FILE)

    if not UUID_RE.fullmatch(uuid):
        return False
    if not REALITY_KEY_RE.fullmatch(private):
        return False
    if not REALITY_KEY_RE.fullmatch(public):
        return False
    if not token or len(token) < 20 or len(token) > 256:
        return False
    try:
        ids = json.loads(ids_raw)
    except Exception:
        return False
    if not isinstance(ids, list) or len(ids) != 3:
        return False
    return all(isinstance(x, str) and SHORT_ID_RE.fullmatch(x) for x in ids)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_seal() -> dict:
    return {
        "schema": 1,
        "files": {path.name: file_sha256(path) for path in IDENTITY_FILES},
    }


def write_seal() -> None:
    atomic_write(SEAL_FILE, json.dumps(build_seal(), sort_keys=True, separators=(",", ":")))


def seal_valid() -> bool:
    raw = read_nonempty(SEAL_FILE)
    if not raw:
        return False
    try:
        obj = json.loads(raw)
        if obj.get("schema") != 1 or obj.get("files") != build_seal()["files"]:
            return False
        return True
    except Exception:
        return False


def identity_fingerprint() -> str:
    """Return a non-secret stable fingerprint for deployment log verification."""
    uuid = read_nonempty(UUID_FILE)
    public = read_nonempty(PUB_FILE)
    digest = hashlib.sha256(f"{uuid}\n{public}".encode()).hexdigest()
    return digest[:16]


def emit_identity_status(state: str) -> None:
    print(f"PERSISTENT_VOLUME={D}")
    print("PERSISTENT_VOLUME_MOUNT=PASS")
    print(f"NODE_IDENTITY={state}")
    print(f"NODE_IDENTITY_FINGERPRINT={identity_fingerprint()}")


def validate_rotation_id(value: str) -> str:
    value = value.strip()
    if not ROTATION_ID_RE.fullmatch(value):
        raise SystemExit(
            "FATAL: SUBSCRIPTION_TOKEN_ROTATE_ID must match YYYYMMDD-NNN "
            "with NNN from 001 to 999"
        )
    date_part, sequence = value.split("-", 1)
    try:
        datetime.datetime.strptime(date_part, "%Y%m%d")
    except ValueError:
        raise SystemExit("FATAL: SUBSCRIPTION_TOKEN_ROTATE_ID contains an invalid Gregorian date")
    if not 1 <= int(sequence) <= 999:
        raise SystemExit("FATAL: SUBSCRIPTION_TOKEN_ROTATE_ID sequence must be 001-999")
    return value


def requested_rotation_id() -> str:
    raw = os.environ.get("SUBSCRIPTION_TOKEN_ROTATE_ID", "").strip()
    if not raw:
        return ""
    return validate_rotation_id(raw)


def read_rotation_state() -> dict | None:
    raw = read_nonempty(ROTATION_STATE_FILE)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        raise SystemExit("FATAL: subscription token rotation state is corrupt; refusing to rotate")
    if not isinstance(obj, dict) or obj.get("schema") != 1:
        raise SystemExit("FATAL: subscription token rotation state is invalid; refusing to rotate")
    last_id = obj.get("last_rotation_id")
    token_sha256 = obj.get("token_sha256")
    if not isinstance(last_id, str) or not ROTATION_ID_RE.fullmatch(last_id):
        raise SystemExit("FATAL: subscription token rotation state has an invalid rotation ID")
    if not isinstance(token_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", token_sha256):
        raise SystemExit("FATAL: subscription token rotation state has an invalid token digest")
    return obj


def write_rotation_state(rotation_id: str) -> None:
    token = read_nonempty(TOKEN_FILE)
    if not token:
        raise SystemExit("FATAL: rotated subscription token is missing")
    state = {
        "schema": 1,
        "last_rotation_id": rotation_id,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    atomic_write(ROTATION_STATE_FILE, json.dumps(state, sort_keys=True, separators=(",", ":")))


def rotate_subscription_token_if_requested() -> None:
    rotation_id = requested_rotation_id()
    if not rotation_id:
        return

    state = read_rotation_state()
    if state is not None and state["last_rotation_id"] == rotation_id:
        current_token = read_nonempty(TOKEN_FILE)
        current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
        if current_hash != state["token_sha256"]:
            raise SystemExit(
                "FATAL: subscription token does not match the recorded rotation state; refusing to rotate"
            )
        print(f"SUBSCRIPTION_TOKEN_ROTATION=ALREADY_APPLIED request_id={rotation_id}")
        return

    # Deliberate exception to normal identity immutability: only the
    # subscription token changes. UUID, REALITY keys and Short IDs are never
    # regenerated or modified by this operation.
    new_token = secrets.token_urlsafe(32)
    atomic_write(TOKEN_FILE, new_token)
    if not identity_complete():
        raise SystemExit("FATAL: subscription token rotation produced an invalid identity set")
    write_seal()
    if not seal_valid():
        raise SystemExit("FATAL: subscription token rotation produced an invalid integrity seal")
    write_rotation_state(rotation_id)
    print(f"SUBSCRIPTION_TOKEN_ROTATION=ROTATED request_id={rotation_id}")


def generate_identity() -> None:
    uuid = subprocess.check_output(["xray", "uuid"], text=True).strip()
    if not UUID_RE.fullmatch(uuid):
        raise RuntimeError("xray generated an invalid UUID")

    raw = subprocess.check_output(["xray", "x25519"], text=True, stderr=subprocess.STDOUT)
    private = public = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("PrivateKey:"):
            private = line.split(":", 1)[1].strip()
        elif line.startswith("Password (PublicKey):"):
            public = line.split(":", 1)[1].strip()
        elif line.startswith("Password:"):
            public = line.split(":", 1)[1].strip()
        elif line.startswith("PublicKey:"):
            public = line.split(":", 1)[1].strip()
    if not REALITY_KEY_RE.fullmatch(private) or not REALITY_KEY_RE.fullmatch(public):
        raise RuntimeError("xray generated an invalid REALITY key pair")

    token = secrets.token_urlsafe(32)
    ids = [secrets.token_hex(6) for _ in range(3)]

    atomic_write(UUID_FILE, uuid)
    atomic_write(PRIV_FILE, private)
    atomic_write(PUB_FILE, public)
    atomic_write(TOKEN_FILE, token)
    atomic_write(IDS_FILE, json.dumps(ids, separators=(",", ":")))


def write_marker() -> None:
    atomic_write(MARKER, "identity initialized")


def main() -> None:
    require_persistent_mount()

    marked = MARKER.is_file()
    complete = identity_complete()
    rotation_id = requested_rotation_id()

    if marked:
        if not complete:
            raise SystemExit(
                "FATAL: node identity was previously initialized but is missing or invalid; "
                "refusing to rotate identity"
            )
        if SEAL_FILE.exists():
            if not seal_valid():
                raise SystemExit(
                    "FATAL: node identity integrity seal mismatch; refusing to rotate identity"
                )
            rotate_subscription_token_if_requested()
            emit_identity_status("REUSED")
            return
        # One-time seal migration for the identity created by the previous
        # identity-init revision. No identity value is changed or regenerated.
        if rotation_id:
            raise SystemExit(
                "FATAL: subscription token rotation requires an existing integrity seal; "
                "remove SUBSCRIPTION_TOKEN_ROTATE_ID and allow the one-time seal migration first"
            )
        write_seal()
        emit_identity_status("REUSED")
        return

    if rotation_id:
        raise SystemExit(
            "FATAL: SUBSCRIPTION_TOKEN_ROTATE_ID is only valid after the persistent identity "
            "has been initialized and sealed"
        )

    if complete:
        write_seal()
        write_marker()
        emit_identity_status("REUSED_INITIALIZED")
        return

    if any(p.exists() for p in IDENTITY_FILES) or SEAL_FILE.exists():
        raise SystemExit(
            "FATAL: partial or invalid node identity found on an uninitialized volume; "
            "refusing to mix, repair, or replace identity"
        )

    generate_identity()
    if not identity_complete():
        raise SystemExit("FATAL: node identity initialization did not produce a complete identity set")
    write_seal()
    write_marker()
    emit_identity_status("INITIALIZED")


if __name__ == "__main__":
    main()
