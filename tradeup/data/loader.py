"""Skin database: fetch, normalize, index. UTF-8 pinned throughout."""
from __future__ import annotations
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional, Union
from ..fsutil import atomic_write_text
from ..models import Rarity, Skin

CSGO_API_MIRRORS = (
    "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json",
    "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en/skins.json",
)
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT/"data"/"skins.json"
SAMPLE_PATH = ROOT/"data"/"sample_skins.json"
SOUVENIR_CRATE_HINT = "souvenir package"

class SyncError(RuntimeError): pass

def derive_qualities(item: dict) -> tuple[bool, bool]:
    """(stattrak_capable, souvenir_capable) from CRATE membership.

    The upstream `souvenir` boolean is set on nearly every finish and is
    useless. Souvenir comes from tournament packages, StatTrak from cases.
    """
    crates = [(c.get("name") or "") for c in (item.get("crates") or [])
              if isinstance(c, dict)]
    if crates:
        sv = any(SOUVENIR_CRATE_HINT in c.lower() for c in crates)
        st = any(SOUVENIR_CRATE_HINT not in c.lower() for c in crates)
        return (True, True) if (sv and st) else (st, sv)
    st = bool(item.get("stattrak", False)); sv = bool(item.get("souvenir", False))
    return (True, False) if (st and sv) else (st, sv)

class SkinDB:
    def __init__(self, skins: Iterable[Skin]):
        self.skins = list(skins)
        self._id = {s.id: s for s in self.skins}
        self._nm = {s.name.lower(): s for s in self.skins}
        self._cr: dict = {}
        for s in self.skins:
            self._cr.setdefault((s.collection,int(s.rarity)), []).append(s)
    def by_id(self, i): return self._id.get(i)
    def by_name(self, n): return self._nm.get(n.lower())
    def pool(self, c, r): return self._cr.get((c,int(r)), [])
    def collections(self, r=None):
        return sorted({s.collection for s in self.skins}) if r is None \
            else sorted({c for c,x in self._cr if x == int(r)})
    def tradeable_collections(self, r):
        n = r.next_tier()
        return [] if n is None else [c for c in self.collections(r) if self.pool(c,n)]
    def __len__(self): return len(self.skins)
    def quality_sanity(self) -> list[str]:
        w, n = [], len(self.skins)
        if not n: return ["database is empty"]
        sv = sum(1 for s in self.skins if s.souvenir_capable)
        if sv/n > 0.90:
            w.append(f"{sv}/{n} ({sv/n:.0%}) marked Souvenir-capable -- almost "
                     "certainly wrong; those exist only for tournament collections")
        both = sum(1 for s in self.skins if s.souvenir_capable and s.stattrak_capable)
        if both/n > 0.50:
            w.append(f"{both}/{n} skins flagged both StatTrak- and Souvenir-capable; "
                     "the two are mutually exclusive in practice, check the parser")
        return w
    def save(self, path=DB_PATH) -> Path:
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        pl = []
        for s in self.skins:
            d = asdict(s); d["rarity"] = int(s.rarity); pl.append(d)
        atomic_write_text(path, json.dumps(pl, ensure_ascii=False, indent=1))
        return path
    @classmethod
    def load(cls, path=None):
        p = Path(path) if path else (DB_PATH if DB_PATH.exists() else SAMPLE_PATH)
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        return cls(Skin(id=d["id"], name=d["name"], rarity=Rarity(int(d["rarity"])),
            collection=d["collection"], min_float=float(d["min_float"]),
            max_float=float(d["max_float"]),
            stattrak_capable=bool(d.get("stattrak_capable", True)),
            souvenir_capable=bool(d.get("souvenir_capable",
                                        d.get("souvenir_only", False))))
            for d in raw)

def parse_skins_payload(raw: list) -> SkinDB:
    skins, seen = [], set()
    for item in raw:
        if not isinstance(item, dict): continue
        try: rarity = Rarity.parse((item.get("rarity") or {}).get("name",""))
        except ValueError: continue
        colls = item.get("collections") or []
        if not colls: continue
        name = item.get("name") or ""
        if name.endswith(")") and "(" in name:
            name = name[:name.rfind("(")].strip()
        mn, mx = item.get("min_float"), item.get("max_float")
        if mn is None or mx is None: continue
        st, sv = derive_qualities(item)
        for c in colls:
            cn = c.get("name") or ""
            if not cn: continue
            sid = f"{name}|{cn}"
            if sid in seen: continue
            seen.add(sid)
            skins.append(Skin(sid, name, rarity, cn, float(mn), float(mx), st, sv))
    if not skins:
        raise SyncError("no usable skins parsed -- upstream schema may have changed")
    return SkinDB(skins)

def fetch_skins_payload(url=None, timeout=60.0) -> list:
    import httpx
    urls = [url] if url else list(CSGO_API_MIRRORS)
    probs = []
    for u in urls:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True,
                    headers={"User-Agent":"cs2-tradeup-lab/2.0",
                             "Accept":"application/json"}) as c:
                r = c.get(u)
            if r.status_code != 200:
                probs.append(f"HTTP {r.status_code} from {u}"); continue
            body = r.text.lstrip()
            if not body or body[0] not in "[{":
                probs.append(f"{u} returned non-JSON: {body[:80]!r}"); continue
            d = r.json()
            if isinstance(d, list) and d: return d
            probs.append(f"{u}: unexpected shape")
        except Exception as e:
            probs.append(f"{u}: {type(e).__name__}: {e}")
    raise SyncError("Could not download the skin database. Tried:\n  - "
        + "\n  - ".join(probs)
        + f"\n\nWorkaround: download it in a browser from\n  {CSGO_API_MIRRORS[0]}\n"
          "then run:  tradeup sync-db --from-file skins.json")

def sync_from_csgo_api(url=None, timeout=60.0, from_file=None) -> SkinDB:
    if from_file is not None:
        raw = json.loads(Path(from_file).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SyncError(f"{from_file} is not a CSGO-API skins.json array")
        return parse_skins_payload(raw)
    return parse_skins_payload(fetch_skins_payload(url, timeout))
