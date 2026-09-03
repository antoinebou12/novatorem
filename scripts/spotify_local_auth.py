#!/usr/bin/env python3
"""
Local Spotify OAuth helper using Spotify-approved loopback redirect.

Dashboard Redirect URI must be exactly:
  http://127.0.0.1:8888/callback

Reads SPOTIFY_CLIENT_ID / SPOTIFY_SECRET_ID from .env.local (gitignored),
captures the auth code locally, writes a new refresh token, optionally
updates Vercel. Never prints secret values.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_LOCAL = ROOT / ".env.local"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8888
SCOPES = "user-read-currently-playing,user-read-recently-played"
TOKEN_URL = "https://accounts.spotify.com/api/token"


def load_env() -> dict[str, str]:
    vals: dict[str, str] = {}
    if ENV_LOCAL.exists():
        for line in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            vals[key.strip()] = value.strip().strip('"').strip("'")
    return vals


def upsert_env(updates: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    other_lines: list[str] = []
    if ENV_LOCAL.exists():
        for line in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                existing[key.strip()] = value.strip()
            elif line.strip():
                other_lines.append(line)
    existing.update(updates)
    lines = other_lines[:]
    for key in ("SPOTIFY_CLIENT_ID", "SPOTIFY_SECRET_ID", "SPOTIFY_REFRESH_TOKEN"):
        if key in existing and existing[key]:
            lines.append(f"{key}={existing[key]}")
    for key, value in existing.items():
        if key.startswith("SPOTIFY_"):
            continue
        lines.append(f"{key}={value}")
    ENV_LOCAL.write_text("\n".join(lines) + "\n", encoding="utf-8")


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def vercel_set(name: str, value: str) -> None:
    for env_name in ("production", "preview", "development"):
        completed = subprocess.run(
            [
                "npx",
                "--yes",
                "vercel@latest",
                "env",
                "add",
                name,
                env_name,
                "--force",
                "--cwd",
                str(ROOT),
            ],
            input=value + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            err = re.sub(r"[A-Za-z0-9_\-]{16,}", "[REDACTED]", completed.stderr or completed.stdout or "")
            raise RuntimeError(f"Failed to set {name} on {env_name}: {err[:300]}")
        print(f"Updated {name} ({env_name})")


def deploy_prod() -> None:
    completed = subprocess.run(
        ["npx", "--yes", "vercel@latest", "deploy", "--prod", "--yes", "--cwd", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    out = re.sub(r"[A-Za-z0-9_\-]{30,}", "[REDACTED]", (completed.stdout or "") + "\n" + (completed.stderr or ""))
    print(out[-800:])
    if completed.returncode != 0:
        raise RuntimeError("Vercel deploy failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-vercel", action="store_true")
    args = parser.parse_args()

    vals = load_env()
    client_id = vals.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = vals.get("SPOTIFY_SECRET_ID", "").strip()
    if not client_id or not client_secret:
        print("Missing SPOTIFY_CLIENT_ID / SPOTIFY_SECRET_ID in .env.local", file=sys.stderr)
        return 1

    result: dict[str, str | None] = {"code": None, "error": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (query.get("code") or [None])[0]
            result["error"] = (query.get("error") or [None])[0]
            ok = bool(result["code"]) and not result["error"]
            body = (
                b"<html><body><h1>Auth captured. You can close this tab.</h1></body></html>"
                if ok
                else b"<html><body><h1>Auth failed.</h1></body></html>"
            )
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *_args) -> None:
            return

    auth_url = (
        "https://accounts.spotify.com/authorize"
        f"?client_id={urllib.parse.quote(client_id)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(SCOPES)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    )

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"Listening on {REDIRECT_URI}")
    print("Opening Spotify authorize page...")
    print(f"Redirect URI in use: {REDIRECT_URI}")
    webbrowser.open(auth_url)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    deadline = time.time() + 300
    while time.time() < deadline and result["code"] is None and result["error"] is None:
        time.sleep(0.25)
    server.shutdown()
    thread.join(timeout=2)

    if result["error"]:
        print(f"OAuth error: {result['error']}", file=sys.stderr)
        return 1
    if not result["code"]:
        print("Timed out waiting for Spotify login/approve.", file=sys.stderr)
        return 1

    print("Code captured (hidden). Exchanging for refresh token...")
    try:
        tokens = exchange_code(client_id, client_secret, str(result["code"]))
    except Exception as exc:
        print(f"Token exchange failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("No refresh_token in Spotify response", file=sys.stderr)
        return 1

    upsert_env({"SPOTIFY_REFRESH_TOKEN": refresh})
    print(f"Saved refresh token to .env.local (len={len(refresh)})")

    if args.update_vercel:
        print("Updating Vercel env (values hidden)...")
        vercel_set("SPOTIFY_CLIENT_ID", client_id)
        vercel_set("SPOTIFY_SECRET_ID", client_secret)
        vercel_set("SPOTIFY_REFRESH_TOKEN", refresh)
        print("Redeploying production...")
        deploy_prod()

    print("Done. No secrets printed.")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
