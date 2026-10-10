"""One-off backfill: fill the `networks` (streaming platform) field for series
uploaded before the fix that persists it (tmdb.py already computed it from
TMDB watch_providers MX, but it was never passed into TVShowSchema/the DB).

Run once from the project root:
    python -m Backend.scripts.backfill_series_networks
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
        collection = db["tv"]
        async for show in collection.find({"tmdb_id": {"$ne": None}}):
            total += 1
            tmdb_id = show["tmdb_id"]
            try:
                det = await tmdb.details("tv", tmdb_id)
            except Exception as e:
                LOGGER.warning(f"[backfill-networks] fetch failed for '{show.get('title')}' ({tmdb_id}): {e}")
                continue
            if not det:
                continue
            networks = list(getattr(det, "networks", None) or [])
            if networks and networks != (show.get("networks") or []):
                await collection.update_one({"_id": show["_id"]}, {"$set": {"networks": networks}})
                updated += 1
                LOGGER.info(f"[backfill-networks] '{show.get('title')}' ({db_key}) -> {networks}")

    LOGGER.info(f"[backfill-networks] done. {updated}/{total} shows updated.")
    await Backend.db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
