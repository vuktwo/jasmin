import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def update_env(env_path: Path, key: str, value: str) -> None:
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    new_line = f"{key}={value}"
    key_found = False

    for index, line in enumerate(lines):
        # Allow whitespace before the key, but do not match similarly named keys.
        if line.lstrip().startswith(f"{key}="):
            lines[index] = new_line
            key_found = True
            break

    if not key_found:
        lines.append(new_line)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Updated {env_path}: {key}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python update_env.py <tunnel_url>", file=sys.stderr)
        sys.exit(1)

    tunnel_url = sys.argv[1].strip().rstrip("/")
    if not tunnel_url.startswith("https://") or not tunnel_url.endswith(".trycloudflare.com"):
        print(f"Invalid Cloudflare Tunnel URL: {tunnel_url}", file=sys.stderr)
        sys.exit(1)

    redirect_uri = f"{tunnel_url}/auth/discord/callback"
    update_env(ENV_FILE, "DISCORD_REDIRECT_URI", redirect_uri)
