"""Skin images, from the same free source as the skin database.

`skins.json` from CSGO-API already carries an `image` field per finish, so
no extra API and no key is needed -- the images are a byproduct of a sync
you already run.

    python -m tradeup.cli sync-images

writes `data/images.json` as {skin name: image url}. The web app loads it
lazily and degrades to a placeholder when a name is missing, so nothing
breaks if you never run it.

Images are hotlinked from Steam's CDN (community.cloudflare.steamstatic.com
or community.akamai.steamstatic.com). They are served to your browser
directly, so this costs your server nothing and adds no API calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

ROOT = Path(__file__).resolve().parents[1]
IMAGES_PATH = ROOT / "data" / "images.json"

# 1x1 transparent gif -- used when a skin has no image, so the layout does
# not jump and no broken-image icon appears
PLACEHOLDER = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


class ImageIndex:
    """name -> image URL, with graceful fallback."""

    def __init__(self, mapping: Optional[dict[str, str]] = None):
        self.map: dict[str, str] = mapping or {}
        # index without the "| " so lookups are forgiving
        self._loose = {self._key(k): v for k, v in self.map.items()}

    @staticmethod
    def _key(name: str) -> str:
        return name.lower().replace("\u2122", "").replace("★", "").strip()

    @classmethod
    def load(cls, path: Union[str, Path] = IMAGES_PATH) -> "ImageIndex":
        p = Path(path)
        if not p.exists():
            return cls({})
        try:
            return cls(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return cls({})

    def get(self, skin_name: str) -> str:
        """Image URL for a finish name like 'AK-47 | Redline'."""
        if skin_name in self.map:
            return self.map[skin_name]
        v = self._loose.get(self._key(skin_name))
        return v or PLACEHOLDER

    def __len__(self) -> int:
        return len(self.map)


def build_from_payload(raw: list) -> dict[str, str]:
    """Extract {name: image} from a CSGO-API skins.json payload."""
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        # strip a trailing "(Wear)" if the entry carries one
        if name.endswith(")") and "(" in name:
            name = name[: name.rfind("(")].strip()
        img = item.get("image")
        if name and img and name not in out:
            out[name] = img
    return out


def save(mapping: dict[str, str], path: Union[str, Path] = IMAGES_PATH) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, ensure_ascii=False, indent=0),
                 encoding="utf-8")
    return p
