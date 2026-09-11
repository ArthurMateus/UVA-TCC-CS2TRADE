"""Skin/collection database loading and indexing.

Source (free, no API key, community maintained):
    https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json

The older `bymykel.github.io/CSGO-API/api/...` host no longer serves JSON --
it answers with an HTML error page, which a bare `.json()` turns into a
confusing "Expecting value" JSONDecodeError. Mirrors are tried in order and
the body is validated before parsing.

ENCODING: every read/write pins `encoding="utf-8"`. Skin names contain CJK
and other non-Latin characters and Windows defaults text I/O to cp1252,
which cannot encode them. Never drop the explicit encoding.

SOUVENIR: since the 22 May 2026 update Souvenir items are valid trade-up
inputs, so Souvenir collections are NOT filtered out -- most Consumer and
Industrial supply lives in tournament/map collections.

Do NOT trust the upstream `souvenir` boolean on its own: in the current
payload it is set on essentially every finish, which would claim a
"Souvenir <skin>" listing exists for all ~1400 entries. The structural
signal is the CRATE: a souvenir variant exists where the skin is
distributed through a "... Souvenir Package". `souvenir_capable` is derived
from that, with the flag used only as a fallback when crate data is missing.
The market is the final arbiter -- a unit is only built if a "Souvenir ..."
listing actually returns a price.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional, Union

from ..models import Rarity, Skin

CSGO_API_MIRRORS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json",
    "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en/skins.json",
    "https://bymykel.github.io/CSGO-API/api/en/skins.json",  # legacy, usually dead
)
CSGO_API_SKINS = CSGO_API_MIRRORS[0]

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "skins.json"
SAMPLE_PATH = DATA_DIR / "sample_skins.json"

SOUVENIR_CRATE_HINT = "souvenir package"


class SyncError(RuntimeError):
    """Raised with actionable guidance when the skin DB cannot be downloaded."""


def _crate_names(item: dict) -> list[str]:
    return [(c.get("name") or "")
            for c in (item.get("crates") or []) if isinstance(c, dict)]


def derive_qualities(item: dict) -> tuple[bool, bool]:
    """(stattrak_capable, souvenir_capable) for one CSGO-API entry.

    Souvenir and StatTrak are mutually exclusive in CS2: StatTrak comes from
    weapon cases, Souvenir from tournament packages, and no item is both.
    Crate membership tells us which distribution a finish belongs to, and is
    far more reliable than the upstream booleans.
    """
    crates = _crate_names(item)
    if crates:
        souvenir = any(SOUVENIR_CRATE_HINT in c.lower() for c in crates)
        stattrak = any(SOUVENIR_CRATE_HINT not in c.lower() for c in crates)
        if souvenir and stattrak:
            # appears in both a case and a souvenir package: allow both
            return True, True
        return stattrak, souvenir
    # No crate data (many map-collection finishes). Fall back to the flags,
    # but never claim both -- that combination does not exist in game.
    st = bool(item.get("stattrak", False))
    sv = bool(item.get("souvenir", False))
    if st and sv:
        return True, False
    return st, sv


class SkinDB:
    """In-memory index over the skin universe."""

    def __init__(self, skins: Iterable[Skin]):
        self.skins: list[Skin] = list(skins)
        self._by_id: dict[str, Skin] = {s.id: s for s in self.skins}
        self._by_name: dict[str, Skin] = {s.name.lower(): s for s in self.skins}
        self._by_coll_rarity: dict[tuple[str, int], list[Skin]] = {}
        for s in self.skins:
            self._by_coll_rarity.setdefault((s.collection, int(s.rarity)), []).append(s)

    # -- lookups ----------------------------------------------------------
    def by_id(self, sid: str) -> Optional[Skin]:
        return self._by_id.get(sid)

    def by_name(self, name: str) -> Optional[Skin]:
        return self._by_name.get(name.lower())

    def pool(self, collection: str, rarity: Rarity) -> list[Skin]:
        """Every skin of `rarity` inside `collection` -- the outcome pool."""
        return self._by_coll_rarity.get((collection, int(rarity)), [])

    def collections(self, rarity: Optional[Rarity] = None) -> list[str]:
        if rarity is None:
            return sorted({s.collection for s in self.skins})
        return sorted({c for (c, r) in self._by_coll_rarity if r == int(rarity)})

    def tradeable_collections(self, input_rarity: Rarity) -> list[str]:
        """Collections with BOTH inputs at `input_rarity` and outputs above."""
        nxt = input_rarity.next_tier()
        if nxt is None:
            return []
        return [c for c in self.collections(input_rarity) if self.pool(c, nxt)]

    def __len__(self) -> int:
        return len(self.skins)

    def quality_sanity(self) -> list[str]:
        """Warn when derived quality flags look implausible.

        A near-100% souvenir rate means the upstream boolean was trusted
        blindly; souvenir variants only exist for tournament/map collections,
        so a realistic share is well under half the universe.
        """
        warnings: list[str] = []
        n = len(self.skins)
        if not n:
            return ["database is empty"]
        sv = sum(1 for s in self.skins if s.souvenir_capable)
        both = sum(1 for s in self.skins if s.souvenir_capable and s.stattrak_capable)
        if sv / n > 0.90:
            warnings.append(
                f"{sv}/{n} ({sv/n:.0%}) skins marked Souvenir-capable. That is "
                "almost certainly wrong -- Souvenir variants only exist for "
                "tournament/map collections.")
        if both / max(n, 1) > 0.50:
            warnings.append(
                f"{both}/{n} skins marked both StatTrak- and Souvenir-capable. "
                "These are mutually exclusive for a given drop source.")
        return warnings

    # -- persistence ------------------------------------------------------
    def save(self, path: Union[str, Path] = DB_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for s in self.skins:
            d = asdict(s)
            d["rarity"] = int(s.rarity)
            payload.append(d)
        # encoding is mandatory: skin names include non-cp1252 characters
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Optional[Union[str, Path]] = None) -> "SkinDB":
        p = Path(path) if path else (DB_PATH if DB_PATH.exists() else SAMPLE_PATH)
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        return cls(
            Skin(
                id=d["id"], name=d["name"], rarity=Rarity(int(d["rarity"])),
                collection=d["collection"],
                min_float=float(d["min_float"]), max_float=float(d["max_float"]),
                stattrak_capable=bool(d.get("stattrak_capable", True)),
                # tolerate databases written before souvenir support
                souvenir_capable=bool(
                    d.get("souvenir_capable", d.get("souvenir_only", False))),
            )
            for d in raw
        )


# ---------------------------------------------------------------------------
# Sync from the public CSGO-API
# ---------------------------------------------------------------------------
def _fetch_json(url: str, timeout: float) -> list:
    """GET a URL and return parsed JSON, with real diagnostics on failure."""
    import httpx

    headers = {
        "User-Agent": "cs2-tradeup-lab/1.0 (+https://github.com/ByMykel/CSGO-API)",
        "Accept": "application/json, text/plain, */*",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
        r = c.get(url)

    if r.status_code != 200:
        raise SyncError(f"HTTP {r.status_code} from {url}")
    body = r.text.lstrip()
    if not body:
        raise SyncError(f"Empty body from {url}")
    if body[0] not in "[{":
        # Almost always an HTML 404/redirect page. Show the start of it so the
        # cause is obvious instead of a bare "Expecting value".
        ctype = r.headers.get("content-type", "?")
        raise SyncError(f"{url} returned {ctype}, not JSON. First bytes: {body[:120]!r}")
    try:
        data = r.json()
    except ValueError as e:
        raise SyncError(f"{url} returned malformed JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise SyncError(f"{url} returned an unexpected payload shape")
    return data


def fetch_skins_payload(url: Optional[str] = None, timeout: float = 60.0) -> list:
    """Fetch the raw skins payload, trying each mirror in turn."""
    urls = [url] if url else list(CSGO_API_MIRRORS)
    problems: list[str] = []
    for u in urls:
        try:
            return _fetch_json(u, timeout)
        except SyncError as e:
            problems.append(str(e))
        except Exception as e:  # network/DNS/TLS
            problems.append(f"{u}: {type(e).__name__}: {e}")
    raise SyncError(
        "Could not download the skin database. Tried:\n  - " + "\n  - ".join(problems)
        + "\n\nWorkaround: download the file manually in a browser from\n  "
        + CSGO_API_MIRRORS[0]
        + "\nthen run:  python -m tradeup.cli sync-db --from-file skins.json")


def parse_skins_payload(raw: list) -> SkinDB:
    """Normalize a CSGO-API `skins.json` payload into a SkinDB."""
    skins: list[Skin] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            rarity = Rarity.parse((item.get("rarity") or {}).get("name", ""))
        except ValueError:
            continue
        colls = item.get("collections") or []
        if not colls:
            continue  # case-only / no collection -> cannot be a trade-up input
        name = item.get("name") or ""
        # CSGO-API names include the wear in some entries; strip it
        if name.endswith(")") and "(" in name:
            name = name[: name.rfind("(")].strip()
        mn, mx = item.get("min_float"), item.get("max_float")
        if mn is None or mx is None:
            continue
        st_cap, sv_cap = derive_qualities(item)
        for coll in colls:
            cname = coll.get("name") or ""
            if not cname:
                continue
            sid = f"{name}|{cname}"
            if sid in seen:
                continue
            seen.add(sid)
            skins.append(Skin(
                id=sid, name=name, rarity=rarity, collection=cname,
                min_float=float(mn), max_float=float(mx),
                stattrak_capable=st_cap, souvenir_capable=sv_cap,
            ))
    if not skins:
        raise SyncError(
            "Payload downloaded but no usable skins were parsed -- the upstream "
            "schema likely changed (expected: name, rarity, collections, "
            "min_float, max_float).")
    return SkinDB(skins)


def sync_from_csgo_api(
    url: Optional[str] = None,
    timeout: float = 60.0,
    from_file: Optional[Union[str, Path]] = None,
) -> SkinDB:
    """Download (or read) + normalize the public skin database."""
    if from_file is not None:
        raw = json.loads(Path(from_file).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SyncError(f"{from_file} is not a CSGO-API skins.json array")
        return parse_skins_payload(raw)
    return parse_skins_payload(fetch_skins_payload(url, timeout))
