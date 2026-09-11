"""New `sync-images` command for tradeup/cli.py.

Paste this function into `tradeup/cli.py` and add this import at the top:

    from .images import IMAGES_PATH, build_from_payload, save as save_images
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

# from .data.loader import SyncError, fetch_skins_payload
# from .images import IMAGES_PATH, build_from_payload, save as save_images
# console = _safe_console()


@app.command("sync-images")
def sync_images(
    out: Path = typer.Option(IMAGES_PATH, help="Where to write images.json"),
    url: Optional[str] = typer.Option(None, help="Override the source URL"),
    from_file: Optional[Path] = typer.Option(
        None, "--from-file", help="Parse a manually downloaded skins.json"),
    timeout: float = typer.Option(60.0),
):
    """Extract skin image URLs from the same CSGO-API payload as sync-db.

    No extra API and no key: `skins.json` already carries an `image` field
    per finish. The web UI hotlinks them straight from Steam's CDN, so this
    costs your server nothing.
    """
    import json as _json

    try:
        if from_file is not None:
            raw = _json.loads(Path(from_file).read_text(encoding="utf-8"))
        else:
            raw = fetch_skins_payload(url, timeout)
    except SyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    mapping = build_from_payload(raw)
    if not mapping:
        console.print("[yellow]No image URLs found -- the upstream schema "
                      "may have changed (expected an 'image' field).[/yellow]")
        raise typer.Exit(1)
    p = save_images(mapping, out)
    console.print(f"[green]Saved {len(mapping)} image URLs to {p}[/green]")
    sample = next(iter(mapping.items()))
    console.print(f"[dim]e.g. {sample[0]} -> {sample[1][:70]}...[/dim]")
