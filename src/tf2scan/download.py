from pathlib import Path
from urllib.parse import urlparse

from .ytdlp_options import with_node


def resolve_stream(video):
    """Resolve a VOD to one video-only media URL without retaining a download."""
    from yt_dlp import YoutubeDL

    with YoutubeDL(
        with_node(
            format="bestvideo[height<=720]/bestvideo",
            noplaylist=True,
            quiet=True,
            skip_download=True,
            retries=2,
        )
    ) as ydl:
        info = ydl.extract_info(video["source_url"], download=False)
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ValueError("Live streams are unsupported; index a bounded VOD")
    duration = info.get("duration") or video.get("duration_s")
    if not isinstance(duration, (int, float)) or not 0 < duration < float("inf"):
        raise ValueError("A finite VOD duration is required for bounded scanning")
    url = info.get("url")
    if not url:
        raise ValueError("yt-dlp did not resolve a video-only stream URL")
    dimensions = {"width": info.get("width"), "height": info.get("height")}
    if not all(isinstance(value, int) and value > 0 for value in dimensions.values()):
        dimensions = None
    host = urlparse(video["source_url"]).hostname
    return url, float(duration), dimensions, host, info.get("http_headers") or {}


def download(store, video):
    from yt_dlp import YoutubeDL

    if video["local_path"] and Path(video["local_path"]).is_file():
        return Path(video["local_path"])
    if video["source_url"].startswith("file:"):
        raise FileNotFoundError(video["local_path"])
    folder = store.root / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    options = with_node(
        format="bestvideo[height<=720]",
        noplaylist=True,
        outtmpl=str(folder / "%(id)s.%(ext)s"),
        download_archive=str(store.root / "download-archive.txt"),
        continuedl=True,
        retries=3,
        fragment_retries=3,
        quiet=True,
    )
    # Archive entries outlive retained files. Explicit reprocessing must be able to redownload.
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(video["source_url"], download=False)
        expected = Path(ydl.prepare_filename(info))
        if not expected.is_file():
            key = ydl._make_archive_id(info)
            if key:
                ydl.archive.discard(key)
            ydl.process_info(info)
        if not expected.is_file():
            raise RuntimeError(f"Download did not create {expected}")
    store.update_video(
        video["id"],
        local_path=str(expected),
        managed_download=1,
        status="scanned" if video["status"] == "scanned" else "downloaded",
        error=None,
        download_bytes=expected.stat().st_size,
    )
    return expected


def cleanup_downloads(store):
    """Called only after a successful report; never delete user-owned local inputs."""
    for video in store.rows("SELECT * FROM videos WHERE status='scanned' AND managed_download=1"):
        if video["local_path"]:
            path = Path(video["local_path"]).resolve()
            if path.is_relative_to((store.root / "downloads").resolve()):
                path.unlink(missing_ok=True)
                store.update_video(video["id"], local_path=None)
