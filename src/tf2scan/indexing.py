import hashlib
import logging
import re
from pathlib import Path

from .frames import probe

log = logging.getLogger(__name__)


def rejection(info, filters):
    uploaded = info.get("upload_date")
    for key, before in (("date_from", True), ("date_to", False)):
        bound = filters.get(key)
        if bound:
            if not uploaded:
                return "upload date unavailable"
            bound = str(bound).replace("-", "")
            if (before and uploaded < bound) or (not before and uploaded > bound):
                return "outside date range"
    duration = info.get("duration")
    for key, below in (("min_duration_s", True), ("max_duration_s", False)):
        if filters.get(key) is not None:
            if duration is None:
                return "duration unavailable"
            if (below and duration < filters[key]) or (not below and duration > filters[key]):
                return "outside duration range"
    title = info.get("title", "")
    if filters.get("title_include") and not re.search(
        filters["title_include"], title, re.IGNORECASE
    ):
        return "title does not match inclusion filter"
    if filters.get("title_exclude") and re.search(filters["title_exclude"], title, re.IGNORECASE):
        return "title matches exclusion filter"
    if info.get("is_live") or info.get("live_status") == "is_upcoming":
        return "live or upcoming video"
    return None


def add_local(store, config, path, profile=None):
    path = Path(path).resolve()
    stream, duration = probe(path)
    video_id = "local-" + hashlib.sha256(str(path).encode()).hexdigest()[:20]
    name = config.profile(override=profile)[0] if config.data.get("pipeline") == "legacy_hud" else None
    store.upsert_video(
        {
            "id": video_id,
            "source_url": path.as_uri(),
            "title": path.stem,
            "duration_s": duration,
            "resolution": stream["height"],
            "local_path": str(path),
            "hud_profile": name,
            "status": "downloaded",
        }
    )
    return video_id


def index_sources(store, config, sources=None):
    from yt_dlp import YoutubeDL

    failures = 0
    indexed = 0
    options = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "retries": 3,
        "ignoreerrors": False,
    }
    with YoutubeDL(options) as discovery, YoutubeDL({**options, "extract_flat": False}) as detail:

        def visit(info):
            nonlocal indexed, failures
            if not info:
                return
            if "entries" in info:
                for entry in info["entries"]:
                    if entry:
                        try:
                            visit(entry)
                        except Exception:
                            failures += 1
                            log.exception("Could not index entry %s", entry.get("id"))
                return
            if info.get("_type") in ("url", "url_transparent") or not info.get("formats"):
                info = detail.extract_info(info.get("webpage_url") or info["url"], download=False)
                if info and "entries" in info:
                    visit(info)
                    return
            if not info:
                return
            reason = rejection(info, config.data.get("filters", {}))
            name = (config.profile(info.get("channel_id"))[0]
                    if config.data.get("pipeline") == "legacy_hud" else None)
            heights = [f.get("height") or 0 for f in info.get("formats", [])]
            store.upsert_video(
                {
                    "id": info["id"],
                    "source_url": info["webpage_url"],
                    "channel_id": info.get("channel_id"),
                    "title": info.get("title", info["id"]),
                    "upload_date": info.get("upload_date"),
                    "duration_s": info.get("duration"),
                    "resolution": max(heights, default=0),
                    "hud_profile": name,
                    "status": "skipped" if reason else "pending",
                    "error": reason,
                }
            )
            indexed += 1

        for source in sources if sources is not None else config.data.get("sources", []):
            try:
                visit(discovery.extract_info(source, download=False))
            except Exception:
                failures += 1
                log.exception("Could not index %s", source)
    return indexed, failures
