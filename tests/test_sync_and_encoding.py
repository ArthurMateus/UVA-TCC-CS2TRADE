"""Regressions for the sync path and for text encoding on Windows.

`Path.write_text()` without an explicit `encoding` uses the OS default --
cp1252 on a default Windows install -- which cannot represent the CJK
characters in several CS2 finish names (e.g. U+58F1). That was a hard crash:

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u58f1'
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from tradeup.data.loader import (
    SkinDB,
    SyncError,
    fetch_skins_payload,
    parse_skins_payload,
    sync_from_csgo_api,
)
from tradeup.models import Rarity, Skin

CJK = "\u58f1"  # the exact character from the reported traceback
TRICKY = f"AK-47 | {CJK} Nihon\u2122 \u2605 caf\u00e9"

PROJECT = Path(__file__).resolve().parents[1]


# --- sync -------------------------------------------------------------
def test_parse_payload_matches_upstream_schema(tmp_path):
    payload = [
        {  # gloves: no collection -> skipped, cannot be a trade-up input
            "name": "\u2605 Hand Wraps | Spruce DDPAT", "min_float": 0.06,
            "max_float": 0.8, "rarity": {"name": "Extraordinary"},
            "collections": [], "stattrak": False, "souvenir": False,
        },
        {
            "name": "AK-47 | Redline", "min_float": 0.1, "max_float": 0.7,
            "rarity": {"name": "Classified"}, "stattrak": True, "souvenir": False,
            "collections": [{"name": "The 2018 Inferno Collection"}],
        },
        {  # in two collections -> one entry each, they have different pools
            "name": "P250 | Asiimov", "min_float": 0.0, "max_float": 1.0,
            "rarity": {"name": "Restricted"}, "stattrak": True, "souvenir": False,
            "collections": [{"name": "The Phoenix Collection"},
                            {"name": "The Chroma Collection"}],
        },
        {"name": "Broken", "rarity": {"name": "Covert"},
         "collections": [{"name": "X"}]},
        "not-a-dict",
    ]
    db = parse_skins_payload(payload)
    names = {s.id for s in db.skins}
    assert "AK-47 | Redline|The 2018 Inferno Collection" in names
    assert "P250 | Asiimov|The Phoenix Collection" in names
    assert "P250 | Asiimov|The Chroma Collection" in names
    assert not any("Hand Wraps" in n for n in names)       # no collection
    assert not any(n.startswith("Broken") for n in names)  # no float data
    assert len(db) == 3

    f = tmp_path / "skins.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    assert len(sync_from_csgo_api(from_file=f)) == 3

    with pytest.raises(SyncError):
        parse_skins_payload([{"name": "x"}])


def test_html_error_page_raises_readable_error(monkeypatch):
    """The old github.io host answers with HTML -> not a JSONDecodeError."""

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            return httpx.Response(
                404, text="<!DOCTYPE html><html>404 Not Found</html>",
                headers={"content-type": "text/html"},
                request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with pytest.raises(SyncError) as e:
        fetch_skins_payload()
    msg = str(e.value)
    assert "HTTP 404" in msg and "--from-file" in msg


# --- encoding ---------------------------------------------------------
def test_save_and_load_roundtrip_non_latin_names(tmp_path):
    """The exact crash from the report: CJK skin name -> save() -> load()."""
    db = SkinDB([Skin(f"{TRICKY}|Coll", TRICKY, Rarity.MILSPEC,
                      "Coll\u2122", 0.0, 1.0)])
    out = tmp_path / "skins.json"
    db.save(out)

    data = out.read_bytes()
    assert CJK.encode("utf-8") in data      # real UTF-8...
    assert b"\\u58f1" not in data           # ...not ASCII escapes

    back = SkinDB.load(out)
    assert back.skins[0].name == TRICKY
    assert back.skins[0].collection == "Coll\u2122"


def test_save_survives_a_cp1252_default_locale(tmp_path, monkeypatch):
    """Simulate Windows: make the OS default cp1252 and save anyway."""
    import locale

    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a: "cp1252")
    if hasattr(locale, "getencoding"):
        monkeypatch.setattr(locale, "getencoding", lambda *a: "cp1252")

    db = SkinDB([Skin(f"{TRICKY}|C", TRICKY, Rarity.MILSPEC, "C", 0.0, 1.0)])
    out = tmp_path / "skins.json"
    db.save(out)  # must not raise
    assert SkinDB.load(out).skins[0].name == TRICKY


def test_price_snapshot_roundtrips_non_latin_names(tmp_path):
    from tradeup.prices.steam import PriceBook, PriceCache, Quote

    cache = PriceCache(tmp_path / "p.sqlite")
    name = f"Souvenir {TRICKY} (Factory New)"
    cache.put(Quote(name, 1.23, 1.50, 42, 0.0), 1)
    snap = tmp_path / "snap.json"
    assert cache.export(snap, 1) == 1
    assert CJK.encode("utf-8") in snap.read_bytes()

    q = PriceBook.load(snap).get(name)
    assert q is not None and q.median == 1.50


def test_old_database_with_souvenir_only_key_still_loads(tmp_path):
    """Databases written before souvenir support must not break on load."""
    legacy = [{
        "id": "X|C", "name": "X", "rarity": 0, "collection": "C",
        "min_float": 0.0, "max_float": 1.0,
        "stattrak_capable": False, "souvenir_only": True,
    }]
    p = tmp_path / "old.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")
    assert SkinDB.load(p).skins[0].souvenir_capable is True


# --- price cache ------------------------------------------------------
def test_miss_cache_prevents_refetching_dead_names(tmp_path):
    """Steam 500s for names with no listings; do not re-fetch them forever."""
    from tradeup.prices.steam import PriceCache

    c = PriceCache(tmp_path / "m.sqlite")
    c.put_miss("Nonexistent | Thing (Factory New)", 7)
    assert c.is_known_miss("Nonexistent | Thing (Factory New)", 7, 3600) is True
    assert c.is_known_miss("Something Else (Factory New)", 7, 3600) is False
    # expired misses are retried
    assert c.is_known_miss("Nonexistent | Thing (Factory New)", 7, -1) is False


@pytest.mark.parametrize("path", sorted((PROJECT / "tradeup").rglob("*.py")))
def test_no_unencoded_text_io_anywhere(path):
    """Static guard: every text read/write in the package pins an encoding."""
    src = path.read_text(encoding="utf-8")
    offenders = []
    for m in re.finditer(r"\.(read_text|write_text)\(", src):
        tail = src[m.end(): m.end() + 220].split("\n\n")[0]
        if "encoding" not in tail:
            offenders.append(src[:m.start()].count("\n") + 1)
    for m in re.finditer(r"(?<![\w.])open\(", src):
        tail = src[m.end(): m.end() + 220].split("\n\n")[0]
        if "encoding" not in tail and '"b' not in tail and "'b" not in tail:
            offenders.append(src[:m.start()].count("\n") + 1)
    assert not offenders, f"{path.name}: text I/O without encoding at {offenders}"
