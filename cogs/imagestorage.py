"""All disk access lives here. Cogs never touch paths or json directly.

Layout:
    storage/<guild_id>/guild.json
    storage/<guild_id>/saves.json
    storage/<guild_id>/<name>.<ext>   (the image, untouched)
    storage/<guild_id>/<name>.json    (its metadata)
"""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

GUILD_LIMIT_BYTES = 1024 ** 3  # 1GB per guild
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
RESERVED_NAMES = {"guild", "saves"}  # would collide with guild.json / saves.json


# ---------- errors ----------

class StorageError(Exception):
    pass


class DuplicateImage(StorageError):
    def __init__(self, existing_name: str):
        super().__init__(existing_name)
        self.existing_name = existing_name


class StorageFull(StorageError):
    pass


# ---------- helpers ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(raw: str | None) -> str | None:
    """Lowercase + validate. Returns None if the name isn't allowed."""
    name = (raw or "").strip().lower()
    if not NAME_RE.match(name) or name in RESERVED_NAMES:
        return None
    return name


def clean_tags(raw_tags) -> list[str]:
    """Lowercase, strip, drop empties, dedupe (keeps order)."""
    seen, out = set(), []
    for tag in raw_tags:
        tag = tag.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    # write to a temp file first so a crash can't leave half a json behind
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    tmp.replace(path)


# ---------- per-guild store ----------

class GuildStore:
    def __init__(self, path: Path, guild_id: int):
        self.path = path
        self.guild_id = guild_id
        self.lock = asyncio.Lock()
        self._guild_file = path / "guild.json"
        self._saves_file = path / "saves.json"

    # --- registration ---

    def is_registered(self) -> bool:
        return self._guild_file.exists()

    async def register(
        self,
        guild_name: str | None = None,
        icon_url: str | None = None,
    ) -> bool:
        """Returns False if this guild was already registered."""
        async with self.lock:
            if self.is_registered():
                return False
            self.path.mkdir(parents=True, exist_ok=True)
            _write_json(self._saves_file, {})
            # guild.json goes last, so is_registered() is only true when setup is complete
            _write_json(self._guild_file, {
                "guild_id": self.guild_id,
                "name": guild_name or f"Guild {self.guild_id}",
                "icon_url": icon_url,
                "registered_at": now_iso(),
                "image_count": 0,
                "whitelist": [],
                "storage": {"used_bytes": 0, "limit_bytes": GUILD_LIMIT_BYTES},
            })
            return True

    async def update_guild_profile(
        self,
        guild_name: str,
        icon_url: str | None,
    ) -> None:
        """Store the display details Discord gives us for the local gallery."""
        async with self.lock:
            if not self.is_registered():
                return
            guild = self.read_guild()
            guild["name"] = guild_name
            guild["icon_url"] = icon_url
            _write_json(self._guild_file, guild)

    # --- reading ---

    def read_guild(self) -> dict:
        return _read_json(self._guild_file)

    def read_saves(self) -> dict:
        return _read_json(self._saves_file)

    def health(self) -> float:
        """Fraction of the storage limit used (0.0 - 1.0)."""
        s = self.read_guild()["storage"]
        return s["used_bytes"] / s["limit_bytes"]

    def has_space(self, size: int) -> bool:
        s = self.read_guild()["storage"]
        return s["used_bytes"] + size <= s["limit_bytes"]

    def _meta_path(self, name: str) -> Path:
        return self.path / f"{name}.json"

    def get_meta(self, name: str) -> dict | None:
        p = self._meta_path(name)
        return _read_json(p) if p.exists() else None

    def image_path(self, meta: dict) -> Path:
        return self.path / f"{meta['name']}.{meta['ext']}"

    def list_images(self, tag: str | None = None) -> list[dict]:
        tag = tag.strip().lower() if tag else None
        out = []
        for f in self.path.glob("*.json"):
            if f.stem in RESERVED_NAMES:
                continue
            meta = _read_json(f)
            if tag is None or tag in meta["tags"]:
                out.append(meta)
        return sorted(out, key=lambda m: m["name"])

    def leaderboard(self) -> list[tuple[int, int]]:
        """[(user_id, count), ...] highest first. Cog slices top/bottom."""
        saves = self.read_saves()
        rows = [(int(uid), d["count"]) for uid, d in saves.items() if d["count"] > 0]
        return sorted(rows, key=lambda r: r[1], reverse=True)

    # --- whitelist (who can delete) ---

    def is_whitelisted(self, user_id: int) -> bool:
        return user_id in self.read_guild()["whitelist"]

    async def whitelist_add(self, user_id: int) -> bool:
        async with self.lock:
            guild = self.read_guild()
            if user_id in guild["whitelist"]:
                return False
            guild["whitelist"].append(user_id)
            _write_json(self._guild_file, guild)
            return True

    async def whitelist_remove(self, user_id: int) -> bool:
        async with self.lock:
            guild = self.read_guild()
            if user_id not in guild["whitelist"]:
                return False
            guild["whitelist"].remove(user_id)
            _write_json(self._guild_file, guild)
            return True

    # --- images ---

    def _unique_name(self, name: str) -> str:
        if not self._meta_path(name).exists():
            return name
        n = 1
        while self._meta_path(f"{name}{n}").exists():
            n += 1
        return f"{name}{n}"

    async def save_image(self, name: str, data: bytes, ext: str, user_id: int) -> dict:
        """Saves the image + metadata. `name` must already be cleaned.

        Raises StorageFull / DuplicateImage. If the name was taken, the returned
        meta["name"] will differ from `name` (cat -> cat1).
        """
        sha = hashlib.sha256(data).hexdigest()
        ext = re.sub(r"[^a-z0-9]", "", ext.lower()) or "png"

        async with self.lock:
            guild = self.read_guild()
            storage = guild["storage"]
            if storage["used_bytes"] + len(data) > storage["limit_bytes"]:
                raise StorageFull()

            for existing in self.list_images():
                if existing["sha256"] == sha:
                    raise DuplicateImage(existing["name"])

            final = self._unique_name(name)
            now = now_iso()

            (self.path / f"{final}.{ext}").write_bytes(data)
            meta = {
                "name": final,
                "ext": ext,
                "saved_by": user_id,
                "saved_at": now,
                "size_bytes": len(data),
                "sha256": sha,
                "tags": [],
            }
            _write_json(self._meta_path(final), meta)

            guild["image_count"] += 1
            storage["used_bytes"] += len(data)
            _write_json(self._guild_file, guild)

            saves = self.read_saves()
            entry = saves.setdefault(str(user_id), {"count": 0, "last_save": None})
            entry["count"] += 1
            entry["last_save"] = now
            _write_json(self._saves_file, saves)

            return meta

    async def add_tags(self, name: str, tags) -> tuple[list[str], list[str]] | None:
        """Returns (added, skipped), or None if the image doesn't exist."""
        tags = clean_tags(tags)
        async with self.lock:
            meta = self.get_meta(name)
            if meta is None:
                return None
            added = [t for t in tags if t not in meta["tags"]]
            skipped = [t for t in tags if t in meta["tags"]]
            meta["tags"].extend(added)
            _write_json(self._meta_path(name), meta)
            return added, skipped

    async def delete_image(self, name: str) -> dict | None:
        """Deletes image + metadata and fixes the counts. Returns the old meta."""
        async with self.lock:
            meta = self.get_meta(name)
            if meta is None:
                return None

            self.image_path(meta).unlink(missing_ok=True)
            self._meta_path(name).unlink(missing_ok=True)

            guild = self.read_guild()
            guild["image_count"] = max(0, guild["image_count"] - 1)
            storage = guild["storage"]
            storage["used_bytes"] = max(0, storage["used_bytes"] - meta["size_bytes"])
            _write_json(self._guild_file, guild)

            saves = self.read_saves()
            entry = saves.get(str(meta["saved_by"]))
            if entry:
                entry["count"] = max(0, entry["count"] - 1)
                _write_json(self._saves_file, saves)

            return meta


# ---------- top level ----------

class Storage:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self._guilds: dict[str, GuildStore] = {}

    def guild(self, guild_id: int) -> GuildStore:
        key = str(guild_id)
        if key not in self._guilds:
            self._guilds[key] = GuildStore(self.base_dir / key, guild_id)
        return self._guilds[key]
