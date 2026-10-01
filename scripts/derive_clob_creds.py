"""Derive CLOB API credentials for a Polymarket email/Google (proxy) account.

Run it yourself from the repository root:

    python -m pip install py-clob-client
    python scripts/derive_clob_creds.py

It asks for your profile (proxy) address and your exported private key
(Polymarket -> Settings -> Export Private Key; input is hidden), derives the
CLOB API key/secret/passphrase, and writes them plus the signer identity into
the repo-root ``.env``. The private key is used only in this process: it is
never printed, logged, or written to ``.env``.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
CLOB_HOST = "https://clob.polymarket.com"
POLY_PROXY = 1  # email / Google sign-in accounts


def _update_env(path: Path, values: dict[str, str]) -> None:
    """Set ``values`` in a dotenv file, keeping every other line as is."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining and not line.lstrip().startswith("#"):
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _clipboard() -> str:
    """Current clipboard text (Windows), or '' when unavailable. Never printed."""
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def main() -> int:
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("py-clob-client is not installed for this Python. Run:\n"
              f'  "{sys.executable}" -m pip install py-clob-client')
        return 1

    proxy = input("Profile (proxy) address, 0x... "
                  "(or copy it and just press Enter): ").replace("\x16", "").strip()
    if not proxy:
        proxy = _clipboard()
    if not (proxy.startswith("0x") and len(proxy) == 42):
        # Never echo the value: it could be the private key copied too early.
        print("That does not look like a 0x address (42 characters). "
              "Copy the profile address and try again.")
        return 1
    print(f"Profile address: {proxy}")
    print("Copy your exported private key, then press Enter here to read it from the")
    print("clipboard (or right-click to paste it first; input stays hidden).")
    key = getpass.getpass("Private key: ").replace("\x16", "").strip()
    if not key:
        key = _clipboard()
    if not key:
        print("No private key entered and the clipboard was empty.")
        return 1
    if not key.startswith("0x"):
        key = "0x" + key

    client = ClobClient(CLOB_HOST, key=key, chain_id=137,
                        signature_type=POLY_PROXY, funder=proxy)
    signer = client.get_address()
    creds = client.create_or_derive_api_creds()
    del key

    _update_env(ENV_PATH, {
        "POLYMARKET_API_KEY": creds.api_key,
        "POLYMARKET_SECRET": creds.api_secret,
        "POLYMARKET_PASSPHRASE": creds.api_passphrase,
        "POLYMARKET_ADDRESS": proxy,
        "POLYMARKET_SIGNER_ADDRESS": signer,
        "POLYMARKET_SIGNATURE_TYPE": str(POLY_PROXY),
    })
    print(f"Signer address: {signer}")
    print(f"Saved API credentials and identity to {ENV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
