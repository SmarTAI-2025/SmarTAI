"""Fail closed before loading application settings or a persistent repository."""
import os
import ipaddress
from pathlib import Path
import socket


def require_isolated_run():
    root_text = os.environ.get("SMARTAI_U3_AUDIT_ROOT", "")
    if not root_text:
        raise RuntimeError("Use run_offline.py; direct execution is disabled")
    root = Path(root_text).resolve()
    if not root.name.startswith("smartai-u3-audit-") or not root.is_dir():
        raise RuntimeError("Invalid disposable U3 root")
    marker = root / ".audit-token"
    if not marker.is_file() or marker.read_text() != os.environ.get("SMARTAI_U3_AUDIT_TOKEN"):
        raise RuntimeError("Missing isolated-run marker")
    expected = {
        "HOME": str(root), "TMPDIR": str(root),
        "SMARTAI_DATABASE_URL": f"sqlite:///{root / 'audit.db'}",
        "SMARTAI_DATABASE_URL_LIGHT": f"sqlite:///{root / 'audit.db'}",
        "SMARTAI_DATABASE_HEAVY": "false", "SMARTAI_STORAGE_BACKEND": "local",
        "SMARTAI_STORAGE_ROOT": str(root / "uploads"),
        "SMARTAI_RUNTIME_ENVIRONMENT": "test", "SMARTAI_SEED_TEST_USERS": "false",
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError("U3 must use only its disposable SQLite/local-storage environment")
    source = Path(os.environ["SMARTAI_U3_SOURCE_ROOT"]).resolve()
    if Path.cwd().resolve() != source or (source / ".env").exists():
        raise RuntimeError("U3 source must be a clean checkout without .env")
    return root


def require_inside(path, root):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError("Worker state, PDF and storage must stay in the disposable U3 root")
    return resolved


def block_network(*, allow_loopback=False):
    attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_dns = socket.getaddrinfo

    def permitted(address):
        if not allow_loopback or not isinstance(address, tuple):
            return False
        try:
            return ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            return False

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not permitted(address):
            attempts.append("connect")
            raise OSError("U3 audit forbids IP connections")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not permitted(address):
            attempts.append("connect_ex")
            raise OSError("U3 audit forbids IP connections")
        return original_connect_ex(sock, address)

    def dns(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            attempts.append("dns")
            raise OSError("U3 audit forbids external DNS")
        return original_dns(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = dns
    return attempts
