"""Offline maintenance UI. Single process, loopback/VPN only, no business workers.

Stop EVERY public/private process, worker, scheduler and old container first.
Run: python -m uvicorn backend.maintenance_main:app --host 127.0.0.1 --port 8001 --workers 1
"""
from backend.private_main import create_private_app
app = create_private_app(maintenance_only=True)
