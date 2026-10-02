#!/usr/bin/env python
"""Preview or execute an offline disposable-business reset. Never use in production."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Without this flag the operation is read-only.")
    parser.add_argument("--fingerprint", default="", help="Exact fingerprint from the reviewed preview.")
    parser.add_argument("--confirm", default="", help="Exact confirmation phrase from that preview.")
    parser.add_argument("--services-stopped", action="store_true", help="Attest ALL public/private services, workers, schedulers and old containers are stopped.")
    parser.add_argument("--allow-object-storage", action="store_true", help="Explicitly allow configured S3 API access; use only an approved disposable bucket.")
    args = parser.parse_args(argv)
    from backend.services.admin_reset import ResetError, execute_reset, preview_reset, scope_from_settings
    from backend.config import settings
    try:
        # Reject before constructing any object client or reading its credentials.
        if settings.runtime_environment not in {"development", "test"} or os.environ.get("SMARTAI_ADMIN_RESET_ENABLED", "false").lower() != "true":
            raise ResetError("reset_disabled_outside_disposable_development")
        client = None
        if settings.storage_backend == "object":
            if not args.allow_object_storage:
                raise ResetError("reset_object_storage_requires_explicit_authorization")
            from backend.storage import build_storage
            client = build_storage().client
        scope = scope_from_settings(object_client=client)
        if args.execute:
            result = execute_reset(scope, fingerprint=args.fingerprint, confirmation=args.confirm, services_stopped=args.services_stopped)
        else:
            result = preview_reset(scope)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except ResetError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "reset_interrupted_inspect_maintenance_marker_before_restart"}), file=sys.stderr)
        return 130
    except Exception:
        # Neither SQLAlchemy nor storage exception text is safe to print here.
        print(json.dumps({"error": "reset_failed_keep_services_stopped"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
