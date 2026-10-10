"""One-off backfill: fill the `networks` (streaming platform) field for movies
uploaded before the platform-category feature existed. Uses the same
flatrate -> buy -> rent -> MX/US fallback chain as new uploads.

Run once from the project root:
    python -m Backend.scripts.backfill_movie_networks
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
        collection = db["movie"]
        async for movie in collection.find({"tmdb_id": {"$ne": None}}):
            total += 1
            tmdb_id = movie["tmdb_id"]
            try:
                det = await tmdb.details("movie", tmdb_id)
            except Exception as e:
                LOGGER.warning(f"[backfill-movie-networks] fetch failed for '{movie.get('title')}' ({tmdb_id}): {e}")
                continue
            if not det:
                continue
            networks = list(getattr(det, "networks", None) or [])
            if networks and networks != (movie.get("networks") or []):
                await collection.update_one({"_id": movie["_id"]}, {"$set": {"networks": networks}})
                updated += 1
                LOGGER.info(f"[backfill-movie-networks] '{movie.get('title')}' ({db_key}) -> {networks}")

    LOGGER.info(f"[backfill-movie-networks] done. {updated}/{total} movies updated.")
    await Backend.db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
