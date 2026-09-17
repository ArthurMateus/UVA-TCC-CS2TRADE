"""Skin images from the same CSGO-API payload sync-db already downloads.

No second API, no key. URLs point at Steam's CDN and are loaded lazily by
the browser, so the server never proxies them.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional, Union

ROOT = Path(__file__).resolve().parents[1]
IMAGES_PATH = ROOT/"data"/"images.json"
PLACEHOLDER = ("data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAA"
               "AAABAAEAAAIBRAA7")

class ImageIndex:
    def __init__(self, mapping: Optional[dict] = None):
        self.map = mapping or {}
        self._loose = {self._key(k): v for k, v in self.map.items()}
    @staticmethod
    def _key(n: str) -> str:
        return n.lower().replace("\u2122","").replace("\u2605","").strip()
    @classmethod
    def load(cls, path: Union[str, Path] = IMAGES_PATH) -> "ImageIndex":
        p = Path(path)
        if not p.exists(): return cls({})
        try: return cls(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError): return cls({})
    def get(self, name: str) -> str:
        return self.map.get(name) or self._loose.get(self._key(name)) or PLACEHOLDER
    def __len__(self): return len(self.map)

def build_from_payload(raw: list) -> dict:
    out = {}
    for item in raw:
        if not isinstance(item, dict): continue
        n = item.get("name") or ""
        if n.endswith(")") and "(" in n: n = n[:n.rfind("(")].strip()
        i = item.get("image")
        if n and i and n not in out: out[n] = i
    return out

def save(mapping: dict, path: Union[str, Path] = IMAGES_PATH) -> Path:
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return p
