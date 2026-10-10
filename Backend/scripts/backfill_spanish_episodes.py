"""One-off backfill: refresh episode titles/overviews to Spanish (Mexico).

Series uploaded before the TVDB es-MX fix kept English episode titles.
This re-fetches each episode from TMDB (client is es-MX) using the
tmdb_id/season/episode already stored on each show, and patches the
title/overview in place if TMDB has a Spanish value.

Run once from the project root:
    python -m Backend.scripts.backfill_spanish_episodes
"""
from __future__ import annotations

import asyncio

import Backend
from Backend.helper.metadata.providers import tmdb
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


async def _backfill_show(collection, show: dict) -> int:
    tmdb_id = show.get("tmdb_id")
    if not tmdb_id:
        return 0

    updated = 0
    for season in show.get("seasons") or []:
        season_number = season.get("season_number")
        for episode in season.get("episodes") or []:
            episode_number = episode.get("episode_number")
            if season_number is None or episode_number is None:
                continue

            try:
                ep = await tmdb.episode_details(tmdb_id, season_number, episode_number)
            except Exception as e:
                LOGGER.warning(
                    f"[backfill] episode fetch failed tmdb_id={tmdb_id} "
                    f"S{season_number}E{episode_number}: {e}"
                )
                continue
            if not ep:
                continue

            new_title = getattr(ep, "name", None)
            new_overview = getattr(ep, "overview", None)
            if not new_title and not new_overview:
                continue
            if new_title == episode.get("title") and new_overview == episode.get("overview"):
                continue

            await collection.update_one(
                {"tmdb_id": tmdb_id, "seasons.season_number": season_number},
                {
                    "$set": {
                        "seasons.$[s].episodes.$[e].title": new_title or episode.get("title"),
                        "seasons.$[s].episodes.$[e].overview": new_overview or episode.get("overview"),
                    }
                },
                array_filters=[
                    {"s.season_number": season_number},
                    {"e.episode_number": episode_number},
                ],
            )
            updated += 1

    return updated


async def main() -> None:
    await Backend.db.connect()
    await SettingsManager.initialize(Backend.db)

    total = 0
    for db_key, db in Backend.db.dbs.items():
        if not db_key.startswith("storage_"):
            continue
        collection = db["tv"]
        async for show in collection.find({"tmdb_id": {"$ne": None}}):
            count = await _backfill_show(collection, show)
            if count:
                total += count
                LOGGER.info(
                    f"[backfill] {db_key}: '{show.get('title')}' "
                    f"({show.get('tmdb_id')}) -> {count} episodes updated"
                )

    LOGGER.info(f"[backfill] done. {total} episodes updated in total.")
    await Backend.db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
