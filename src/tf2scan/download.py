from pathlib import Path


def download(store, video):
    from yt_dlp import YoutubeDL

    if video["local_path"] and Path(video["local_path"]).is_file():
        return Path(video["local_path"])
    if video["source_url"].startswith("file:"):
        raise FileNotFoundError(video["local_path"])
    folder = store.root / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestvideo[height<=720]",
        "noplaylist": True,
        "outtmpl": str(folder / "%(id)s.%(ext)s"),
        "download_archive": str(store.root / "download-archive.txt"),
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "quiet": True,
    }
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
