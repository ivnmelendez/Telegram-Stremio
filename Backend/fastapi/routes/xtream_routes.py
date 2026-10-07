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

#----- A single TiviMate menu open fires several player_api actions back-to-back
#----- (categories, then streams for each). Without this, each one re-sorts the
#----- whole catalog from Mongo. Short TTL: fresh enough, cuts the repeat queries.
_CATALOG_CACHE_TTL = 30
_catalog_cache: dict = {}


async def _cached(key: str, loader) -> list:
    now = time.time()
    cached = _catalog_cache.get(key)
    if cached and now - cached[0] < _CATALOG_CACHE_TTL:
        return cached[1]
    data = await loader()
    _catalog_cache[key] = (now, data)
    return data


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


#----- Fixed ids for the catch-all "Todas las peliculas/series" categories, always
#----- first in the menu. Low fixed values so they never collide with crc32 ids.
ALL_MOVIES_CATEGORY_ID = "1"
ALL_SERIES_CATEGORY_ID = "2"
ALL_MOVIES_CATEGORY_NAME = "Todas las películas"
ALL_SERIES_CATEGORY_NAME = "Todas las series"


async def _all_movies() -> list:
    async def _load():
        data = await db.sort_movies([("updated_on", "desc")], 1, CATALOG_SIZE)
        return data.get("movies", [])
    return await _cached("movies", _load)


async def _all_tv_shows() -> list:
    async def _load():
        data = await db.sort_tv_shows([("updated_on", "desc")], 1, CATALOG_SIZE)
        return data.get("tv_shows", [])
    return await _cached("tv_shows", _load)


#----- Platforms in the same order as _PLATFORM_DISPLAY (provider's own order),
#----- any platform not in that dict falls back to the end, alphabetically.
def _ordered_platforms(networks: set) -> list:
    ordered = [p for p in _PLATFORM_DISPLAY if p in networks]
    extra = sorted(n for n in networks if n not in _PLATFORM_DISPLAY)
    return ordered + extra


#----- One title -> one category, same as a real provider (no duplicate stream
#----- entries per secondary tag). Movie's primary is its first genre.
def _primary_movie_tag(genres: list) -> str | None:
    return genres[0] if genres else None


#----- Series: platform wins over genre when both exist (matches category order
#----- All -> platforms -> genres). Returns (tag, "platform"|"genre") or (None, None).
def _primary_series_tag(genres: list, networks: list) -> tuple:
    if networks:
        ordered = _ordered_platforms(set(networks))
        if ordered:
            return ordered[0], "platform"
    if genres:
        return genres[0], "genre"
    return None, None


#----- Distinct primary genre across the movie catalog (only tags actually
#----- assignable to a title, so the menu never shows an empty category).
async def _movie_categories_set() -> set:
    tags = set()
    for m in await _all_movies():
        tag = _primary_movie_tag(m.get("genres") or [])
        if tag:
            tags.add(tag)
    return tags


#----- Distinct primary genres/platforms across the series catalog, split so the
#----- menu can order platforms before genres.
async def _series_categories_set() -> tuple:
    genre_tags = set()
    platform_tags = set()
    for s in await _all_tv_shows():
        tag, kind = _primary_series_tag(s.get("genres") or [], s.get("networks") or [])
        if kind == "platform":
            platform_tags.add(tag)
        elif kind == "genre":
            genre_tags.add(tag)
    return genre_tags, platform_tags


def _current_year() -> int:
    return datetime.utcnow().year


#----- "Estrenos <year>" is an EXTRA tag on top of a title's primary category
#----- (not instead of it) - a title can match both "Todas" + its genre/platform
#----- + Estrenos at once. Safe: filtered requests just check membership, and the
#----- unfiltered dump always emits one entry regardless of how many tags exist.
def _estrenos_movie_category_id(year: int) -> str:
    return _category_id("movie_special", f"estrenos_{year}")


def _estrenos_series_category_id(year: int) -> str:
    return _category_id("series_special", f"estrenos_{year}")


async def _has_estrenos_movies(year: int) -> bool:
    return any((m.get("release_year") == year) for m in await _all_movies())


async def _has_estrenos_series(year: int) -> bool:
    return any((s.get("release_year") == year) for s in await _all_tv_shows())


#----- Visual formatting seen on a real Xtream provider's series categories —
#----- colored square emoji + "+" instead of "Plus". Display-only: category_id
#----- is always computed from the raw name, so this is safe to extend anytime.
_PLATFORM_DISPLAY = {
    "Netflix": "🟥 Netflix",
    "Disney Plus": "🟦 Disney+",
    "Prime Video": "🟦 Prime Video",
    "Apple TV": "⬛ Apple TV+",
    "HBO Max": "🟪 HBO Max",
    "Paramount Plus": "🟦 Paramount+",
    "ViX": "🟧 ViX",
    "Hulu": "🟩 Hulu",
    "Peacock": "🟨 Peacock",
}


#----- Genre names that need a Spanish label on display (TMDB/TVDB names come in
#----- English). category_id is always computed from the raw name.
_GENRE_DISPLAY = {
    "Sci-Fi & Fantasy": "Ciencia Ficción y Fantasía",
}


def _display_name(name: str) -> str:
    return _PLATFORM_DISPLAY.get(name) or _GENRE_DISPLAY.get(name) or name


async def _list_vod_streams(category_id: str = None) -> list:
    movies = [m for m in await _all_movies() if m.get("imdb_id")]
    #----- One round-trip for every stream id in this response, instead of one
    #----- await per title (slow with thousands of movies in the catalog).
    id_map = await db.upsert_xtream_stream_ids_bulk([(m["imdb_id"], "movie", None, None) for m in movies])
    year = _current_year()

    out = []
    for m in movies:
        imdb_id = m["imdb_id"]
        primary = _primary_movie_tag(m.get("genres") or [])
        primary_id = _category_id("movie_genre", primary) if primary else None
        estreno_id = _estrenos_movie_category_id(year) if m.get("release_year") == year else None
        cat_ids = [ALL_MOVIES_CATEGORY_ID] + ([primary_id] if primary_id else []) + ([estreno_id] if estreno_id else [])
        if category_id and category_id not in cat_ids:
            continue
        #----- Una sola entrada por titulo, SIEMPRE (filtrado o no) - el cliente
        #----- arma su submenu de categorias con get_vod_categories, no escaneando
        #----- este dump, asi que duplicar aca solo infla el catalogo sin razon.
        #----- category_ids expone TODAS las categorias (Todas + genero + Estrenos)
        #----- para clientes que filtran por categoria. Pero algunos clientes (UHF)
        #----- ignoran category_ids y agrupan leyendo category_id singular del dump
        #----- sin filtro - por eso Estrenos gana sobre el genero ahi, para que esos
        #----- clientes tambien la vean poblada (el titulo sigue en su genero via
        #----- cat_ids/category_ids para los clientes que si filtran).
        cat = category_id or estreno_id or primary_id or ALL_MOVIES_CATEGORY_ID
        sid = id_map[f"{imdb_id}:None:None"]
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
            "category_id": cat,
            "category_ids": cat_ids,
            "container_extension": "mkv",
            "custom_sid": "",
            "direct_source": "",
        })
    return out


async def _list_series(category_id: str = None) -> list:
    shows = [s for s in await _all_tv_shows() if s.get("imdb_id")]
    id_map = await db.upsert_xtream_stream_ids_bulk([(s["imdb_id"], "tv", None, None) for s in shows])
    year = _current_year()

    out = []
    for s in shows:
        imdb_id = s["imdb_id"]
        primary, _kind = _primary_series_tag(s.get("genres") or [], s.get("networks") or [])
        primary_id = _category_id("series_tag", primary) if primary else None
        estreno_id = _estrenos_series_category_id(year) if s.get("release_year") == year else None
        cat_ids = [ALL_SERIES_CATEGORY_ID] + ([primary_id] if primary_id else []) + ([estreno_id] if estreno_id else [])
        if category_id and category_id not in cat_ids:
            continue
        #----- Una sola entrada por titulo, SIEMPRE (filtrado o no) - mismo motivo
        #----- que en _list_vod_streams. category_ids: ver comentario equivalente ahi.
        cat = category_id or estreno_id or primary_id or ALL_SERIES_CATEGORY_ID
        sid = id_map[f"{imdb_id}:None:None"]
        out.append({
            "num": sid,
            "series_id": sid,
            "name": s.get("title") or "Untitled",
            "cover": s.get("poster") or "",
            "cover_big": s.get("poster") or "",
            "plot": s.get("description") or "",
            "genre": ", ".join(s.get("genres") or []),
            "releaseDate": f"{s.get('release_year')}-01-01" if s.get("release_year") else "",
            "category_id": cat,
            "category_ids": cat_ids,
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

    sorted_seasons = sorted(tv_doc.get("seasons", []), key=lambda s: s.get("season_number") or 0)

    #----- Gather every playable episode first so the stream-id map can be upserted
    #----- in ONE round-trip instead of one await per episode (slow on heavy series).
    playable = []
    for season in sorted_seasons:
        snum = season.get("season_number")
        sorted_episodes = sorted(season.get("episodes", []), key=lambda e: e.get("episode_number") or 0)
        for ep in sorted_episodes:
            if ep.get("telegram"):
                playable.append((snum, ep))
    id_map = await db.upsert_xtream_stream_ids_bulk(
        [(doc["imdb_id"], "tv", snum, ep.get("episode_number")) for snum, ep in playable]
    )

    seasons_out = []
    episodes_out = {}
    for season in sorted_seasons:
        snum = season.get("season_number")
        seasons_out.append({
            "season_number": snum,
            "name": f"Season {snum}",
            "episode_count": len(season.get("episodes", [])),
        })
        eps = []
        for s, ep in playable:
            if s != snum:
                continue
            enum = ep.get("episode_number")
            eid = id_map[f"{doc['imdb_id']}:{snum}:{enum}"]
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
        genres = await _movie_categories_set()
        year = _current_year()
        cats = [{"category_id": ALL_MOVIES_CATEGORY_ID, "category_name": ALL_MOVIES_CATEGORY_NAME, "parent_id": 0}]
        if await _has_estrenos_movies(year):
            cats.append({
                "category_id": _estrenos_movie_category_id(year),
                "category_name": f"🎬 Estrenos {year}",
                "parent_id": 0,
            })
        cats += [
            {"category_id": _category_id("movie_genre", g), "category_name": _display_name(g), "parent_id": 0}
            for g in sorted(genres)
        ]
        return cats
    if action == "get_series_categories":
        genres, networks = await _series_categories_set()
        year = _current_year()
        cats = [{"category_id": ALL_SERIES_CATEGORY_ID, "category_name": ALL_SERIES_CATEGORY_NAME, "parent_id": 0}]
        if await _has_estrenos_series(year):
            cats.append({
                "category_id": _estrenos_series_category_id(year),
                "category_name": f"🎬 Estrenos {year}",
                "parent_id": 0,
            })
        cats += [
            {"category_id": _category_id("series_tag", p), "category_name": _display_name(p), "parent_id": 0}
            for p in _ordered_platforms(networks)
        ]
        cats += [
            {"category_id": _category_id("series_tag", g), "category_name": _display_name(g), "parent_id": 0}
            for g in sorted(genres)
        ]
        return cats
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
