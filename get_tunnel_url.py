import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "cloudflared.log"
URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def extract_url(timeout=60):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            content = ""

        match = URL_PATTERN.search(content)
        if match:
            return match.group(0)

        time.sleep(1)

    return None


if __name__ == "__main__":
    url = extract_url()
    if url:
        print(url)
    else:
        print("Timed out waiting for a Cloudflare Tunnel URL.", file=sys.stderr)
        sys.exit(1)
