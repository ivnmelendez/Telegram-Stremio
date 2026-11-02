"""One-off backfill: replace every stored logo with a TMDB CDN link
(Spanish first, English fallback). Entries TMDB has no logo for end up empty.

Run once from the project root:
    python -m Backend.scripts.backfill_tmdb_logos
"""
from __future__ import annotations

import asyncio

import Backend
from Backend.helper.metadata.providers import tmdb
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


async def main() -> None:
    await Backend.db.connect()
    await SettingsManager.initialize(Backend.db)

    total = 0
    updated = 0
    for db_key, db in Backend.db.dbs.items():
        if not db_key.startswith("storage_"):
            continue
        for name in ("movie", "tv"):
            collection = db[name]
            async for doc in collection.find({}):
                total += 1
                payload = {
                    "media_type": "movie" if name == "movie" else "tv",
                    "imdb_id": doc.get("imdb_id"),
                    "tmdb_id": doc.get("tmdb_id"),
                    "logo": "",
                }
                try:
                    await tmdb.ensure_tmdb_logo(payload)
                except Exception as e:
                    LOGGER.warning(f"[backfill-logos] failed for '{doc.get('title')}' ({db_key}): {e}")
                    continue
                if payload["logo"] and payload["logo"] != doc.get("logo"):
                    await collection.update_one({"_id": doc["_id"]}, {"$set": {"logo": payload["logo"]}})
                    updated += 1
                    LOGGER.info(f"[backfill-logos] '{doc.get('title')}' ({db_key}) -> {payload['logo']}")

    LOGGER.info(f"[backfill-logos] done. {updated}/{total} updated.")
    await Backend.db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
