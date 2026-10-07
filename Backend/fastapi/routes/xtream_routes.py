import time
import zlib
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from Backend import db
from Backend.fastapi.routes.stremio_routes import get_resolution_priority, parse_size_to_bytes, stream_res_label
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.cf_stream import cf_enabled, cf_stream_url
from Backend.helper.passwords import verify_password
from Backend.helper.settings_manager import SettingsManager

router = APIRouter(tags=["Xtream"])

CATALOG_SIZE = 5000  #----- effectively "all of it" for a personal catalog


#----- Validate Xtream username/password against the linked API token, returning
#----- (token, token_data) or (None, None). Playback itself is re-validated by /dl
#----- via its own verify_token dependency, so this only needs to gate the facade.
async def _authenticate(username: str, password: str):
    if not username or not password:
        return None, None
    token_doc = await db.get_token_by_xtream_username(username)
    if not token_doc or not verify_password(password, token_doc.get("xtream_password_hash") or ""):
        return None, None
    token = token_doc.get("token")
    token_data = await verify_token(token)
    return token, token_data


def _server_info() -> dict:
    base = SettingsManager.current().base_url or ""
    parsed = urlparse(base)
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    return {
        "url": parsed.hostname or base,
        "port": port,
        "https_port": port if parsed.scheme == "https" else "443",
        "server_protocol": parsed.scheme or "https",
        "rtmp_port": "0",
        "timezone": "UTC",
        "timestamp_now": int(time.time()),
        "time_now": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _user_info(token_data: dict, username: str) -> dict:
    expires_at = token_data.get("expires_at")
    exp_ts = int(expires_at.timestamp()) if expires_at else None
    status = "Expired" if (token_data.get("subscription_expired") or token_data.get("limit_exceeded")) else "Active"
    return {
        "username": username,
        "password": "",
        "auth": 1,
        "status": status,
        "exp_date": str(exp_ts) if exp_ts else None,
        "is_trial": "0",
        "active_cons": "0",
        "max_connections": str(token_data.get("max_concurrent_streams") or 0),
        "allowed_output_formats": ["mkv", "mp4", "ts"],
    }


#----- Auto-pick the single best file for a title, honouring the token's quality_filter
#----- (same criteria the Stremio addon uses to order/filter streams by default).
def _pick_best_quality(telegram_list: list, token_data: dict) -> dict | None:
    candidates = [q for q in (telegram_list or []) if q.get("id")]
    if not candidates:
        return None
    config = (token_data or {}).get("config") or {}
    quality_filter = set(config.get("quality_filter") or [])
    if quality_filter:
        filtered = [q for q in candidates if stream_res_label(q.get("name") or "") in quality_filter]
        if filtered:
            candidates = filtered
    candidates.sort(
        key=lambda q: (get_resolution_priority(q.get("name") or q.get("quality") or ""), parse_size_to_bytes(q.get("size") or "")),
        reverse=True,
    )
    return candidates[0]


def _dl_redirect_url(token: str, quality_id: str) -> str:
    if cf_enabled() and SettingsManager.current().cf_stream_mode in ("cloudflare", "both"):
        return cf_stream_url(token, quality_id, "video.mkv")
    return f"{SettingsManager.current().base_url}/dl/{token}/{quality_id}/video.mkv"


#----- Deterministic category id for a genre/platform name. Same scheme as
#----- upsert_xtream_stream_id: stable across requests/restarts, no DB table needed.
def _category_id(prefix: str, name: str) -> str:
    return str(zlib.crc32(f"{prefix}:{name}".encode()) & 0x7FFFFFFF)


async def _all_movies() -> list:
    data = await db.sort_movies([("updated_on", "desc")], 1, CATALOG_SIZE)
    return data.get("movies", [])


async def _all_tv_shows() -> list:
    data = await db.sort_tv_shows([("updated_on", "desc")], 1, CATALOG_SIZE)
    return data.get("tv_shows", [])


#----- Distinct genres across the movie catalog -> [(name, category_id), ...]
async def _movie_categories() -> list:
    genres = set()
    for m in await _all_movies():
        genres.update(m.get("genres") or [])
    return sorted(genres)


#----- Distinct genres UNION streaming platforms across the series catalog —
#----- both live as flat categories in the same list, same as real Xtream panels do.
async def _series_categories() -> list:
    tags = set()
    for s in await _all_tv_shows():
        tags.update(s.get("genres") or [])
        tags.update(s.get("networks") or [])
    return sorted(tags)


async def _list_vod_streams(category_id: str = None) -> list:
    out = []
    for m in await _all_movies():
        imdb_id = m.get("imdb_id")
        if not imdb_id:
            continue
        genres = m.get("genres") or []
        if category_id:
            if not any(_category_id("movie_genre", g) == category_id for g in genres):
                continue
        primary_cat = _category_id("movie_genre", genres[0]) if genres else "0"
        sid = await db.upsert_xtream_stream_id(imdb_id, "movie")
        name = m.get("title") or "Untitled"
        added = str(int(m.get("updated_on").timestamp())) if m.get("updated_on") else ""
        out.append({
            "num": sid,
            "name": name,
            "stream_type": "movie",
            "stream_id": sid,
            "stream_icon": m.get("poster") or "",
            "rating": str(m.get("rating") or ""),
            "rating_5based": round((m.get("rating") or 0) / 2, 1),
            "added": added,
            "is_adult": "0",
            "category_id": primary_cat,
            "container_extension": "mkv",
            "custom_sid": "",
            "direct_source": "",
        })
    return out


async def _list_series(category_id: str = None) -> list:
    out = []
    for s in await _all_tv_shows():
        imdb_id = s.get("imdb_id")
        if not imdb_id:
            continue
        tags = (s.get("genres") or []) + (s.get("networks") or [])
        if category_id:
            if not any(_category_id("series_tag", t) == category_id for t in tags):
                continue
        primary_cat = _category_id("series_tag", tags[0]) if tags else "0"
        sid = await db.upsert_xtream_stream_id(imdb_id, "tv")
        out.append({
            "num": sid,
            "series_id": sid,
            "name": s.get("title") or "Untitled",
            "cover": s.get("poster") or "",
            "cover_big": s.get("poster") or "",
            "plot": s.get("description") or "",
            "genre": ", ".join(s.get("genres") or []),
            "releaseDate": f"{s.get('release_year')}-01-01" if s.get("release_year") else "",
            "category_id": primary_cat,
            "rating": str(s.get("rating") or ""),
            "rating_5based": round((s.get("rating") or 0) / 2, 1),
        })
    return out


async def _series_info(series_id: int) -> dict:
    doc = await db.resolve_xtream_stream_id(series_id)
    if not doc or doc.get("media_type") != "tv":
        return {"seasons": [], "episodes": {}}
    tv_doc = await db.get_media_details(imdb_id=doc["imdb_id"])
    if not tv_doc:
        return {"seasons": [], "episodes": {}}

    seasons_out = []
    episodes_out = {}
    sorted_seasons = sorted(tv_doc.get("seasons", []), key=lambda s: s.get("season_number") or 0)
    for season in sorted_seasons:
        snum = season.get("season_number")
        seasons_out.append({
            "season_number": snum,
            "name": f"Season {snum}",
            "episode_count": len(season.get("episodes", [])),
        })
        eps = []
        sorted_episodes = sorted(season.get("episodes", []), key=lambda e: e.get("episode_number") or 0)
        for ep in sorted_episodes:
            if not ep.get("telegram"):
                continue
            enum = ep.get("episode_number")
            eid = await db.upsert_xtream_stream_id(doc["imdb_id"], "tv", snum, enum)
            eps.append({
                "id": str(eid),
                "episode_num": enum,
                "season": snum,
                "title": ep.get("title") or f"Episode {enum}",
                "container_extension": "mkv",
                "info": {
                    "plot": ep.get("overview") or "",
                    "movie_image": ep.get("episode_backdrop") or tv_doc.get("poster") or "",
                    "releasedate": ep.get("released") or "",
                },
            })
        episodes_out[str(snum)] = eps

    poster = tv_doc.get("poster") or ""
    genres = tv_doc.get("genres") or []
    return {
        "info": {
            "name": tv_doc.get("title"),
            "cover": poster,
            "cover_big": poster,
            "backdrop_path": [tv_doc.get("backdrop")] if tv_doc.get("backdrop") else [],
            "plot": tv_doc.get("description") or "",
            "genre": ", ".join(genres) if genres else "",
            "rating": str(tv_doc.get("rating") or ""),
            "releaseDate": f"{tv_doc.get('release_year')}-01-01" if tv_doc.get("release_year") else "",
        },
        "seasons": seasons_out,
        "episodes": episodes_out,
    }


async def _vod_info(vod_id: int) -> dict:
    doc = await db.resolve_xtream_stream_id(vod_id)
    if not doc or doc.get("media_type") != "movie":
        return {}
    movie = await db.get_media_details(imdb_id=doc["imdb_id"])
    if not movie:
        return {}
    plot = movie.get("description") or ""
    poster = movie.get("poster") or ""
    genres = movie.get("genres") or []
    rating = movie.get("rating") or 0
    release_year = movie.get("release_year")
    tmdb_id = movie.get("tmdb_id")
    name = movie.get("title")
    category_id = _category_id("movie_genre", genres[0]) if genres else "0"
    return {
        "info": {
            "name": name,
            "o_name": movie.get("original_title") or name,
            "movie_image": poster,
            "cover_big": poster,
            "backdrop_path": [movie.get("backdrop")] if movie.get("backdrop") else [],
            "plot": plot,
            "description": plot,
            "genre": ", ".join(genres) if genres else "",
            "rating": str(rating),
            "releasedate": f"{release_year}-01-01" if release_year else "",
            "tmdb_id": str(tmdb_id or ""),
            "tmdb_url": f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id else "",
        },
        "movie_data": {
            "stream_id": vod_id,
            "name": name,
            "added": "",
            "category_id": category_id,
            "container_extension": "mkv",
        },
    }


@router.get("/player_api.php")
@router.post("/player_api.php")
async def player_api(request: Request):
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            params.update(await request.json())
        except Exception:
            pass

    username = params.get("username", "")
    password = params.get("password", "")
    action = params.get("action", "")

    token, token_data = await _authenticate(username, password)
    if not token:
        return JSONResponse({"user_info": {"auth": 0}}, status_code=200)

    if not action:
        return {"user_info": _user_info(token_data, username), "server_info": _server_info()}

    if action == "get_vod_categories":
        genres = await _movie_categories()
        return [{"category_id": _category_id("movie_genre", g), "category_name": g, "parent_id": 0} for g in genres]
    if action == "get_series_categories":
        tags = await _series_categories()
        return [{"category_id": _category_id("series_tag", t), "category_name": t, "parent_id": 0} for t in tags]
    if action in ("get_live_categories", "get_live_streams"):
        return []
    if action == "get_vod_streams":
        return await _list_vod_streams(params.get("category_id"))
    if action == "get_series":
        return await _list_series(params.get("category_id"))
    if action == "get_vod_info":
        try:
            vod_id = int(params.get("vod_id", 0))
        except (TypeError, ValueError):
            return {}
        return await _vod_info(vod_id)
    if action == "get_series_info":
        try:
            series_id = int(params.get("series_id", 0))
        except (TypeError, ValueError):
            return {"seasons": [], "episodes": {}}
        return await _series_info(series_id)

    return []


@router.get("/movie/{username}/{password}/{stream_id}.{ext}")
async def xtream_movie(username: str, password: str, stream_id: int, ext: str):
    token, token_data = await _authenticate(username, password)
    if not token:
        raise HTTPException(status_code=401, detail="Invalid Xtream credentials")

    doc = await db.resolve_xtream_stream_id(stream_id)
    if not doc or doc.get("media_type") != "movie":
        raise HTTPException(status_code=404, detail="Stream not found")

    movie = await db.get_media_details(imdb_id=doc["imdb_id"])
    if not movie or movie.get("type") != "movie":
        raise HTTPException(status_code=404, detail="Movie not found")

    quality = _pick_best_quality(movie.get("telegram") or [], token_data)
    if not quality:
        raise HTTPException(status_code=404, detail="No playable file for this title")

    return RedirectResponse(_dl_redirect_url(token, quality["id"]), status_code=302)


@router.get("/series/{username}/{password}/{episode_id}.{ext}")
async def xtream_episode(username: str, password: str, episode_id: int, ext: str):
    token, token_data = await _authenticate(username, password)
    if not token:
        raise HTTPException(status_code=401, detail="Invalid Xtream credentials")

    doc = await db.resolve_xtream_stream_id(episode_id)
    if not doc or doc.get("media_type") != "tv":
        raise HTTPException(status_code=404, detail="Stream not found")

    episode = await db.get_media_details(
        imdb_id=doc["imdb_id"],
        season_number=doc.get("season_number"),
        episode_number=doc.get("episode_number"),
    )
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")

    quality = _pick_best_quality(episode.get("telegram") or [], token_data)
    if not quality:
        raise HTTPException(status_code=404, detail="No playable file for this episode")

    return RedirectResponse(_dl_redirect_url(token, quality["id"]), status_code=302)
