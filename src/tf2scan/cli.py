import argparse
import logging
import sys
from contextlib import ExitStack, closing
from pathlib import Path

from .config import load_config, positive
from .download import cleanup_downloads, download
from .frames import sample_frames
from .hud import annotate
from .indexing import add_local, index_sources
from .ingestion import ingest
from .query import run_query
from .recognize import OpenOCRRecognizer
from .report import export_report
from .storage import Store

log = logging.getLogger(__name__)


def parser():
    app = argparse.ArgumentParser(description="Build a reusable TF2 killfeed corpus and search it")
    app.add_argument("--verbose", action="store_true")
    commands = app.add_subparsers(dest="command", required=True)
    for name in (
        "index",
        "scan",
        "query",
        "report",
        "calibrate",
        "review",
        "benchmark",
        "delete-video",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("--config", default="config.yaml")
        if name == "index":
            sub.add_argument("config_path", nargs="?")
        elif name == "scan":
            sources = sub.add_mutually_exclusive_group()
            sources.add_argument("--pending", action="store_true")
            sources.add_argument("--video")
            sources.add_argument("--local", type=Path)
            sub.add_argument("--profile")
            sub.add_argument("--fps", type=float)
            sub.add_argument("--chunk-seconds", type=float)
            sub.add_argument("--reprocess", action="store_true")
            sub.add_argument("--name", help="Name to search after scanning; overrides query.target_name")
            sub.add_argument("--alias", action="append", default=[])
        elif name == "query":
            sub.add_argument("--name")
            sub.add_argument("--alias", action="append", default=[])
        elif name == "report":
            sub.add_argument("--query-id", type=int)
            sub.add_argument("--rows", action="store_true")
            sub.add_argument("--text-clusters", action="store_true")
            selection = sub.add_mutually_exclusive_group()
            selection.add_argument("--scan-id", type=int)
            selection.add_argument("--run-id", type=int)
        elif name == "calibrate":
            sub.add_argument("video", help="Local path, indexed ID, or video URL")
            sub.add_argument("--timestamp", type=float, default=0)
            sub.add_argument("--profile")
            sub.add_argument("--output", type=Path, default=Path("calibration.png"))
        elif name == "review":
            sub.add_argument("hit_id", type=int)
            sub.add_argument("status", choices=["confirmed", "rejected", "unreviewed"])
            sub.add_argument("--note", default="")
        elif name == "benchmark":
            sub.add_argument("manifest", type=Path)
            sub.add_argument("--output", type=Path, default=Path("benchmark.json"))
        elif name == "delete-video":
            sub.add_argument("video_id")
    return app


def configured_query(store, config, name=None, aliases=None):
    query = config.data.get("query", {})
    target = name or query.get("target_name")
    if not target:
        return None
    return run_query(store, target, aliases if name else query.get("aliases", []),
                     matching=config.data.get("matching"))


def execute(args):
    config = load_config(getattr(args, "config_path", None) or args.config)
    with Store(config.root) as store, ExitStack() as models:
        if args.command == "index":
            count, failed = index_sources(store, config)
            print(f"Indexed {count} videos; {failed} failures")
            return int(bool(failed))
        if args.command == "scan":
            name = getattr(args, "name", None)
            aliases = getattr(args, "alias", [])
            if name or aliases:
                query = config.data.setdefault("query", {})
                if name:
                    query["target_name"] = name
                    query["aliases"] = aliases
                else:
                    query["aliases"] = [*query.get("aliases", []), *aliases]
            sampling_overrides = {k: v for k, v in
                                  (("fps", args.fps),
                                   ("chunk_seconds", getattr(args, "chunk_seconds", None)))
                                  if v is not None}
            if sampling_overrides:
                config.data.setdefault("sampling", {}).update(sampling_overrides)
            config.effective_scan()
            if args.fps is not None:
                positive(args.fps, "fps")
            if args.local:
                args.video = add_local(store, config, args.local, args.profile)
            if args.video:
                queue = [store.video(args.video)]
            else:
                queue = store.rows(
                    """SELECT * FROM videos WHERE status IN ('pending','downloaded','failed')
                    OR EXISTS (SELECT 1 FROM video_scans s WHERE s.video_id=videos.id
                    AND s.status!='completed') ORDER BY id"""
                )
            recognizer = None
            detector = None
            failures = 0
            for video in queue:
                unfinished = store.rows("SELECT id FROM video_scans WHERE video_id=? AND "
                                        "status!='completed' LIMIT 1", (video["id"],))
                if video["status"] == "scanned" and not args.reprocess and not unfinished:
                    log.info(
                        "Already scanned: %s (use --reprocess to create a new scan)", video["id"]
                    )
                    continue
                try:
                    if args.profile:
                        config.profile(override=args.profile)
                        video["hud_profile"] = args.profile
                    # Check model setup before initiating downloads.
                    if recognizer is None:
                        if config.data["pipeline"] == "legacy_hud":
                            recognizer = OpenOCRRecognizer(config)
                        else:
                            from .paddle_backend import create_models

                            detector, recognizer = create_models(config)
                            models.callback(detector.close)
                            models.callback(recognizer.close)
                    if config.data["pipeline"] == "legacy_hud":
                        path = download(store, video)
                        video["local_path"] = str(path)
                    ingest(
                        store,
                        config,
                        video,
                        recognizer,
                        args.fps,
                        args.reprocess,
                        detector=detector,
                    )
                except Exception as exc:
                    failures += 1
                    # A failed replacement must leave the old completed corpus usable.
                    status = "scanned" if video["status"] == "scanned" else "failed"
                    store.update_video(video["id"], status=status, error=str(exc))
                    log.exception("Video %s failed", video["id"])
            query_id = configured_query(store, config)
            hits = export_report(store, query_id, config.data.get("export_rows", False))
            if not config.data.get("retain_downloads", True):
                cleanup_downloads(store)
            for hit in hits:
                print(
                    f"{hit['video_id']} {hit['start_s']:.1f}s {hit['best_score']:.3f} {hit['best_text']}"
                )
            target = config.data.get("query", {}).get("target_name")
            selected_scans = store.selected_scan_ids()
            cluster_count = len(store.selected_clusters())
            print(f"Saved corpus: {cluster_count} text clusters across "
                  f"{len(selected_scans)} completed video scans.")
            if target:
                print(f"Search target: {target!r}; {len(hits)} candidates. "
                      "Try 'tf2scan query --name NAME' to search another name without rescanning.")
            else:
                print("No search target configured. Use 'tf2scan query --name NAME' "
                      "to search the saved OCR text.")
            print(f"{failures} failures; {config.root / 'report/index.html'}")
            return int(bool(failures))
        if args.command == "query":
            query_id = configured_query(store, config, args.name, args.alias)
            if query_id is None:
                raise ValueError("Set query.target_name or pass --name")
            hits = export_report(store, query_id, config.data.get("export_rows", False))
            print(f"Query {query_id} for {args.name or config.data['query']['target_name']!r}: "
                  f"{len(hits)} candidates; no OCR performed")
        elif args.command == "report":
            hits = export_report(store, args.query_id, args.rows,
                                 text_clusters=args.text_clusters,
                                 scan_id=args.scan_id, run_id=args.run_id)
            print(f"{len(hits)} candidates: {config.root / 'report/index.html'}")
        elif args.command == "review":
            with store.transaction() as db:
                cursor = db.execute(
                    "UPDATE hits SET review_status=?,reviewer_note=? WHERE id=?",
                    (args.status, args.note, args.hit_id),
                )
                if not cursor.rowcount:
                    raise ValueError(f"Unknown hit: {args.hit_id}")
            export_report(store)
        elif args.command == "calibrate":
            if args.timestamp < 0:
                raise ValueError("timestamp must be nonnegative")
            if Path(args.video).is_file():
                video_id = add_local(store, config, args.video, args.profile)
            elif args.video.startswith(("https://", "http://")):
                index_sources(store, config, [args.video])
                videos = store.rows("SELECT id FROM videos WHERE source_url=?", (args.video,))
                if not videos:
                    # yt-dlp canonicalizes short URLs; resolve the canonical ID.
                    from yt_dlp import YoutubeDL

                    with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
                        video_id = ydl.extract_info(args.video, download=False)["id"]
                else:
                    video_id = videos[0]["id"]
            else:
                video_id = args.video
            video = store.video(video_id)
            path = download(store, video)
            _, profile = config.profile(video["channel_id"], args.profile or video["hud_profile"])
            with closing(sample_frames(path, start=args.timestamp, limit=1)) as frames:
                sample = next(frames, None)
            if sample is None:
                raise ValueError("No frame at the requested timestamp")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            sample[1].save(args.output.with_name(args.output.stem + "-original.png"))
            annotate(sample[1], profile).save(args.output)
            print(args.output.resolve())
        elif args.command == "benchmark":
            from .benchmark import benchmark

            result = benchmark(
                args.manifest, OpenOCRRecognizer(config), config.data["scan"], args.output
            )
            print(
                f"{result['crops']} crops; {result['crops_per_second']:.2f} crops/s; {args.output}"
            )
        elif args.command == "delete-video":
            store.delete_video(args.video_id)
            export_report(store, rows=True)
    return 0


def main():
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )
    try:
        status = execute(args)
    except KeyboardInterrupt:
        print("Interrupted; completed videos are preserved.", file=sys.stderr)
        status = 130
    except Exception as exc:
        log.error("%s", exc, exc_info=args.verbose)
        status = 1
    raise SystemExit(status)
