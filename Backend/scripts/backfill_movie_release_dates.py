"""One-off backfill: fill `release_date` (exact TMDB date) for movies uploaded
before the field existed - needed to sort "Estrenos <year>" by real release
date instead of upload date.

Run once from the project root:
    python -m Backend.scripts.backfill_movie_release_dates
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
        async for movie in collection.find({"tmdb_id": {"$ne": None}, "release_date": {"$in": [None, ""]}}):
            total += 1
            tmdb_id = movie["tmdb_id"]
            try:
                det = await tmdb.details("movie", tmdb_id)
            except Exception as e:
                LOGGER.warning(f"[backfill-release-date] fetch failed for '{movie.get('title')}' ({tmdb_id}): {e}")
                continue
            release = getattr(det, "release_date", None) if det else None
            if not release:
                continue
            await collection.update_one({"_id": movie["_id"]}, {"$set": {"release_date": release.isoformat()}})
            updated += 1
            LOGGER.info(f"[backfill-release-date] '{movie.get('title')}' ({db_key}) -> {release.isoformat()}")

    LOGGER.info(f"[backfill-release-date] done. {updated}/{total} movies updated.")
    await Backend.db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
