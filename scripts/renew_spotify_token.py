#!/usr/bin/env python3
"""
Renew a revoked/expired Spotify refresh token and optionally update Vercel.

Spotify refresh tokens expire after 6 months (enforced for existing apps
from July 20, 2026). Run this whenever /api/spotify returns invalid_grant.

Usage:
  python scripts/renew_spotify_token.py
  python scripts/renew_spotify_token.py --update-vercel
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import webbrowser
from base64 import b64encode
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    print("Install dependencies first: pip install requests python-dotenv", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore


# Must match Redirect URIs in the Spotify Developer Dashboard exactly.
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost/callback/")
SCOPES = "user-read-currently-playing,user-read-recently-played"
TOKEN_URL = "https://accounts.spotify.com/api/token"
ROOT = Path(__file__).resolve().parents[1]


def load_credentials() -> tuple[str, str]:
    if load_dotenv:
        load_dotenv(ROOT / ".env.local")
        load_dotenv(ROOT / ".env")

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_SECRET_ID", "").strip()
    if not client_id or not client_secret:
        print(
            "Missing SPOTIFY_CLIENT_ID or SPOTIFY_SECRET_ID. "
            "Put them in .env / .env.local or export them first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return client_id, client_secret


def extract_code(callback_value: str) -> str:
    value = callback_value.strip().strip('"').strip("'")
    if value.startswith("http://") or value.startswith("https://"):
        query = parse_qs(urlparse(value).query)
        if "error" in query:
            raise ValueError(f"Spotify returned error: {query['error'][0]}")
        if "code" not in query:
            raise ValueError("No ?code= found in the callback URL")
        return query["code"][0]
    return value


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    basic = b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError(f"Token exchange failed ({response.status_code}): {response.text}")
    payload = response.json()
    if "refresh_token" not in payload:
        raise RuntimeError("Spotify response did not include a refresh_token")
    return payload


def write_env_file(path: Path, refresh_token: str) -> None:
    lines: list[str] = []
    replaced = False
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("SPOTIFY_REFRESH_TOKEN="):
                lines.append(f"SPOTIFY_REFRESH_TOKEN={refresh_token}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"SPOTIFY_REFRESH_TOKEN={refresh_token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_vercel(refresh_token: str) -> None:
    # Non-interactive env update via stdin
    cmd = [
        "npx",
        "--yes",
        "vercel@latest",
        "env",
        "add",
        "SPOTIFY_REFRESH_TOKEN",
        "production",
        "preview",
        "development",
        "--force",
        "--cwd",
        str(ROOT),
    ]
    print("Updating SPOTIFY_REFRESH_TOKEN on Vercel...")
    completed = subprocess.run(
        cmd,
        input=refresh_token + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise RuntimeError("Failed to update Vercel env. Update it manually in the dashboard.")
    print(completed.stdout.strip() or "Vercel env updated.")
    print("Redeploying production...")
    deploy = subprocess.run(
        ["npx", "--yes", "vercel@latest", "deploy", "--prod", "--yes", "--cwd", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    print(deploy.stdout)
    if deploy.returncode != 0:
        print(deploy.stderr, file=sys.stderr)
        raise RuntimeError("Deploy failed. Run: npx vercel deploy --prod --yes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-vercel",
        action="store_true",
        help="Write the new token to Vercel and redeploy production",
    )
    parser.add_argument(
        "--code",
        help="Authorization code or full https://example.com/callback?code=... URL",
    )
    args = parser.parse_args()

    client_id, client_secret = load_credentials()
    auth_url = (
        "https://accounts.spotify.com/authorize"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&redirect_uri={REDIRECT_URI}"
    )

    if not args.code:
        print("Opening Spotify authorization in your browser...")
        print(auth_url)
        webbrowser.open(auth_url)
        print(
            f"\nAfter approving, your browser will go to {REDIRECT_URI}?code=...\n"
            "(The page may fail to load — that is fine.)\n"
            "Copy the full browser URL (it contains ?code=...) and paste it here.\n"
        )
        callback = input("Callback URL or code: ").strip()
    else:
        callback = args.code

    try:
        code = extract_code(callback)
        tokens = exchange_code(client_id, client_secret, code)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    refresh_token = tokens["refresh_token"]
    print("\nNew SPOTIFY_REFRESH_TOKEN acquired.")
    write_env_file(ROOT / ".env.local", refresh_token)
    if (ROOT / ".env").exists():
        write_env_file(ROOT / ".env", refresh_token)
    print("Saved to .env.local" + (" and .env" if (ROOT / ".env").exists() else ""))

    if args.update_vercel:
        try:
            update_vercel(refresh_token)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print("\nManual fallback:")
            print("  1. Vercel project → Settings → Environment Variables")
            print("  2. Update SPOTIFY_REFRESH_TOKEN")
            print("  3. Redeploy")
            return 1
    else:
        print("\nNext (secrets stay hidden):")
        print("  python scripts/apply_spotify_secrets.py --refresh-token-file .secret_refresh.txt --update-vercel")
        print("Or re-run with --update-vercel now.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
