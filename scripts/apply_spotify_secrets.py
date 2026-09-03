#!/usr/bin/env python3
"""
Apply Spotify secrets to local env + Vercel without printing them.

Share secrets via a file or stdin (never commit the file).

Examples:
  # Paste refresh token when prompted (input is not echoed back by this script's logs)
  python scripts/apply_spotify_secrets.py --refresh-token

  # From a one-line file you create locally (then delete):
  python scripts/apply_spotify_secrets.py --refresh-token-file .secret_refresh.txt --update-vercel

  # Pipe (PowerShell):
  Get-Content .secret_refresh.txt | python scripts/apply_spotify_secrets.py --refresh-token-stdin --update-vercel

  # Update client id / secret too:
  python scripts/apply_spotify_secrets.py --client-id-file .secret_id.txt --client-secret-file .secret_secret.txt --update-vercel
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_LOCAL = ROOT / ".env.local"
ENV_FILE = ROOT / ".env"


def read_secret(value: str | None, file_path: str | None, from_stdin: bool, prompt: str) -> str | None:
    if value:
        return value.strip()
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit(f"Empty secret file: {file_path}")
        return text.splitlines()[0].strip()
    if from_stdin:
        text = sys.stdin.read().strip()
        if not text:
            raise SystemExit("No secret received on stdin")
        return text.splitlines()[0].strip()
    # Interactive prompt (visible in some terminals — prefer file/stdin when possible)
    typed = getpass.getpass(prompt).strip()
    return typed or None


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def vercel_set(name: str, value: str, environments: list[str]) -> None:
    for env_name in environments:
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
            err = completed.stderr or completed.stdout or ""
            err = re.sub(r"[A-Za-z0-9_\-]{16,}", "[REDACTED]", err)
            raise SystemExit(f"Failed to set {name} on Vercel ({env_name}): {err[:400]}")
        print(f"Updated {name} ({env_name})")


def deploy_prod() -> None:
    print("Deploying production...")
    completed = subprocess.run(
        ["npx", "--yes", "vercel@latest", "deploy", "--prod", "--yes", "--cwd", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (completed.stdout or "") + "\n" + (completed.stderr or "")
    out = re.sub(r"[A-Za-z0-9_\-]{30,}", "[REDACTED]", out)
    print(out[-1000:])
    if completed.returncode != 0:
        raise SystemExit("Vercel deploy failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-token", action="store_true", help="Prompt for SPOTIFY_REFRESH_TOKEN")
    parser.add_argument("--refresh-token-value", help=argparse.SUPPRESS)  # avoid casual shell history use
    parser.add_argument("--refresh-token-file")
    parser.add_argument("--refresh-token-stdin", action="store_true")
    parser.add_argument("--client-id-file")
    parser.add_argument("--client-secret-file")
    parser.add_argument("--client-id-stdin", action="store_true")
    parser.add_argument("--client-secret-stdin", action="store_true")
    parser.add_argument("--update-vercel", action="store_true", help="Write secrets to Vercel env")
    parser.add_argument("--deploy", action="store_true", help="Redeploy production after Vercel env update")
    parser.add_argument(
        "--env",
        action="append",
        choices=("production", "preview", "development"),
        help="Vercel environment (repeatable). Default: all three.",
    )
    args = parser.parse_args()

    updates: dict[str, str] = {}

    refresh = None
    if args.refresh_token or args.refresh_token_file or args.refresh_token_stdin or args.refresh_token_value:
        refresh = read_secret(
            args.refresh_token_value,
            args.refresh_token_file,
            args.refresh_token_stdin,
            "SPOTIFY_REFRESH_TOKEN: ",
        )
        if refresh:
            updates["SPOTIFY_REFRESH_TOKEN"] = refresh

    if args.client_id_file or args.client_id_stdin:
        client_id = read_secret(None, args.client_id_file, args.client_id_stdin, "SPOTIFY_CLIENT_ID: ")
        if client_id:
            updates["SPOTIFY_CLIENT_ID"] = client_id

    if args.client_secret_file or args.client_secret_stdin:
        client_secret = read_secret(
            None, args.client_secret_file, args.client_secret_stdin, "SPOTIFY_SECRET_ID: "
        )
        if client_secret:
            updates["SPOTIFY_SECRET_ID"] = client_secret

    if not updates:
        parser.error("No secrets provided. Use --refresh-token / --refresh-token-file / --*-stdin.")

    # Never print secret values — only key names + lengths
    for key, val in updates.items():
        print(f"Received {key} (len={len(val)})")

    upsert_env(ENV_LOCAL, updates)
    print(f"Wrote {ENV_LOCAL.name}")
    if ENV_FILE.exists():
        upsert_env(ENV_FILE, updates)
        print(f"Wrote {ENV_FILE.name}")

    if args.update_vercel:
        envs = args.env or ["production", "preview", "development"]
        for key, val in updates.items():
            vercel_set(key, val, envs)
        deploy_prod()

    print("Done. Secrets were not printed.")
    return 0


if __name__ == "__main__":
    # Ensure cwd-relative paths work when invoked from repo root
    os.chdir(ROOT)
    raise SystemExit(main())
