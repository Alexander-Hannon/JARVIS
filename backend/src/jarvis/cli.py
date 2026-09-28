"""`jarvis` command line: serve | worker | doctor | stt-warmup | transcribe | gen-vapid | setup-key | backup | restore."""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="jarvis")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="Run the API + UI (loopback by default)")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sub.add_parser("worker", help="Run the job/scheduler worker")
    sub.add_parser("doctor", help="Readiness diagnostics")
    sub.add_parser("stt-warmup", help="Download/load the local STT model and measure warm-up")
    tr = sub.add_parser("transcribe", help="Transcribe an audio file locally (diagnostic)")
    tr.add_argument("path")
    sub.add_parser("gen-vapid", help="Generate Web Push VAPID keys for .env")
    sub.add_parser("setup-key", help="Create the token-encryption key file if missing")
    sub.add_parser("backup", help="Create a backup")
    rs = sub.add_parser("restore", help="Restore a backup (neutralizes jobs/approvals, disables schedules)")
    rs.add_argument("backup_id")
    rs.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn

        from jarvis.config import get_settings
        s = get_settings()
        host = a.host or s.host
        if host not in ("127.0.0.1", "localhost", "::1") and not s.remote_mode:
            sys.exit("Refusing non-loopback bind without JARVIS_REMOTE_MODE=true and an HTTPS front (see DEPLOYMENT.md).")
        uvicorn.run("jarvis.api.main:app", host=host, port=a.port or s.port, proxy_headers=False, log_level="info")
    elif a.cmd == "worker":
        from jarvis.worker import main as wm
        wm()
    elif a.cmd == "doctor":
        from jarvis.doctor import main as dm
        dm()
    elif a.cmd == "stt-warmup":
        from jarvis.voice.stt import get_engine, hardware_profile
        print(json.dumps(hardware_profile(), indent=2))
        print(json.dumps(get_engine().warm_up(), indent=2))
    elif a.cmd == "transcribe":
        from jarvis.voice.stt import decode_audio, get_engine
        with open(a.path, "rb") as f:
            d = decode_audio(f.read())
        t = get_engine().transcribe(d)
        print(json.dumps({"text": t.text, "confidence": t.confidence, "latency_ms": t.latency_ms, "audio_s": t.duration_s,
                          "format": d.source_format, "model": t.model}, indent=2))
    elif a.cmd == "gen-vapid":
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        k = ec.generate_private_key(ec.SECP256R1())
        priv = base64.urlsafe_b64encode(k.private_numbers().private_value.to_bytes(32, "big")).rstrip(b"=").decode()
        pub = base64.urlsafe_b64encode(k.public_key().public_bytes(serialization.Encoding.X962,
                                                                   serialization.PublicFormat.UncompressedPoint)).rstrip(b"=").decode()
        print(f"JARVIS_VAPID_PUBLIC_KEY={pub}\nJARVIS_VAPID_PRIVATE_KEY={priv}\n# add both to .env (chmod 600); never commit")
    elif a.cmd == "setup-key":
        from jarvis.security.secrets import ensure_key, key_status
        ensure_key()
        print(key_status())
    elif a.cmd == "backup":
        from jarvis.backup import create_backup
        print(json.dumps(create_backup("cli"), indent=2))
    elif a.cmd == "restore":
        from jarvis.backup import restore, restore_preview
        print(json.dumps(restore_preview(a.backup_id), indent=2))
        if not a.yes:
            sys.exit("Re-run with --yes to restore (a pre-restore backup is taken first).")
        print(json.dumps(restore(a.backup_id), indent=2))


if __name__ == "__main__":
    main()
