import html
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from .geometry import decode_polygon


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def timestamp_url(url, seconds):
    if urlsplit(url).scheme not in ("https", "http"):
        return ""
    return url + ("&" if "?" in url else "?") + f"t={math.floor(seconds)}s"


def evidence_url(store, value):
    if not value:
        return ""
    path = (store.root / value).resolve()
    base = (store.root / "report").resolve()
    return (
        path.relative_to(base).as_posix()
        if base.is_relative_to(store.root.resolve())
        and path.is_relative_to(base)
        and path.is_file()
        else ""
    )


def export_report(
    store, query_id=None, rows=False, *, text_clusters=False, scan_id=None, run_id=None
):
    condition, params = ("WHERE h.query_id=?", (query_id,)) if query_id else ("", ())
    scan_ids = store.selected_scan_ids(scan_id=scan_id, run_id=run_id)
    condition += (
        (" AND " if condition else "WHERE ")
        + "c.video_scan_id IN ("
        + (",".join("?" for _ in scan_ids) or "NULL")
        + ")"
    )
    params = (*params, *scan_ids)
    hits = store.rows(
        f"""SELECT h.*,c.video_id,c.video_scan_id,c.start_s,c.end_s,c.frame_path,
        c.model_version,c.normalization_version,c.hud_profile,c.evidence_timestamp_s,
        c.evidence_row_index,c.representative_observation_id,
        c.evidence_path AS representative_path,c.canonical_text AS representative_text,
        c.motion_summary_json,c.close_reason,v.title,v.source_url,q.target_name,
        s.scan_run_id,r.detector_name,r.detector_version,r.detector_weights_hash,
        r.recognizer_name,r.recognizer_version,r.recognizer_weights_hash,
        r.preprocessing_version,r.geometry_encoding_version,r.software_versions_json,
        r.config_hash,r.config_json,q.matcher_version,q.settings_json
        FROM hits h JOIN text_clusters c ON c.id=h.text_cluster_id
        JOIN videos v ON v.id=c.video_id JOIN queries q ON q.id=h.query_id
        JOIN video_scans s ON s.id=c.video_scan_id JOIN scan_runs r ON r.id=s.scan_run_id
        {condition} ORDER BY h.best_score DESC,c.start_s""",
        params,
    )
    for hit in hits:
        hit["schema_version"] = 2
        hit["timestamp_url"] = timestamp_url(hit["source_url"], hit["start_s"])
        observations = store.rows(
            """SELECT o.id,o.raw_text,o.crop_path,o.polygon_blob,f.timestamp_s,
            f.full_frame_path FROM observations o
            JOIN cluster_observations co ON co.observation_id=o.id
            JOIN sampled_frames f ON f.id=o.sampled_frame_id
            WHERE co.cluster_id=? ORDER BY f.timestamp_s,o.id""",
            (hit["text_cluster_id"],),
        )
        hit["ocr_strings"] = list(dict.fromkeys(o["raw_text"] for o in observations))
        scored = store.rows(
            """SELECT qm.observation_id FROM query_matches qm
            JOIN cluster_observations co ON co.observation_id=qm.observation_id
            WHERE qm.query_id=? AND co.cluster_id=? AND qm.matched_alias=?
            ORDER BY qm.match_score DESC,qm.observation_id LIMIT 1""",
            (hit["query_id"], hit["text_cluster_id"], hit["matched_alias"]),
        )
        matched_id = scored[0]["observation_id"] if scored else None
        matched = next((o for o in observations if o["id"] == matched_id), None)
        representative = next(
            (o for o in observations if o["id"] == hit["representative_observation_id"]), None
        )
        evidence = matched if matched and matched["crop_path"] else representative
        hit["matched_observation_id"] = matched_id
        hit["matched_timestamp_s"] = matched["timestamp_s"] if matched else None
        hit["matched_polygon"] = (
            decode_polygon(matched["polygon_blob"]) if matched and matched["polygon_blob"] else None
        )
        hit["representative_polygon"] = (
            decode_polygon(representative["polygon_blob"])
            if representative and representative["polygon_blob"]
            else None
        )
        hit["matched_crop_path"] = matched["crop_path"] if matched else None
        hit["observation_evidence_available"] = bool(matched and matched["crop_path"])
        hit["evidence_path"] = evidence["crop_path"] if evidence else hit["representative_path"]
        hit["evidence_observation_id"] = evidence["id"] if evidence else None
        hit["evidence_timestamp_s"] = (
            evidence["timestamp_s"] if evidence else hit["evidence_timestamp_s"]
        )
        hit["evidence_text"] = evidence["raw_text"] if evidence else hit["representative_text"]
        hit["frame_path"] = evidence["full_frame_path"] if evidence else hit["frame_path"]
        hit["evidence_kind"] = "matched" if evidence is matched and matched else "representative"
    atomic_write(
        store.root / "hits.jsonl", "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in hits)
    )
    if rows or text_clusters:
        path = store.root / "text_clusters.jsonl"
        temporary = path.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            for row in store.selected_clusters(scan_id=scan_id, run_id=run_id):
                value = dict(row)
                value["schema_version"] = 2
                video = store.video(row["video_id"])
                value.update({key: video[key] for key in ("source_url", "title", "channel_id")})
                scan = store.rows(
                    "SELECT scan_run_id,status FROM video_scans WHERE id=?", (row["video_scan_id"],)
                )[0]
                value["scan_status"] = scan["status"]
                value["scan_run_id"] = scan["scan_run_id"]
                value["run_provenance"] = store.rows(
                    "SELECT * FROM scan_runs WHERE id=?", (scan["scan_run_id"],)
                )[0]
                if value["representative_polygon_blob"] is not None:
                    value["representative_polygon"] = decode_polygon(
                        value["representative_polygon_blob"]
                    )
                else:
                    value["representative_polygon"] = None
                del value["representative_polygon_blob"]
                observations = store.rows(
                    """SELECT o.id,o.raw_text,o.polygon_blob,o.screen_region,
                    o.normalized_width,o.normalized_height,o.detector_confidence,
                    o.ocr_confidence,o.crop_path,o.legacy_evidence_path,
                    f.id AS sampled_frame_id,f.timestamp_s,f.full_frame_path,
                    co.support_score FROM cluster_observations co
                    JOIN observations o ON o.id=co.observation_id
                    JOIN sampled_frames f ON f.id=o.sampled_frame_id
                    WHERE co.cluster_id=? ORDER BY f.timestamp_s,o.id""",
                    (row["id"],),
                )
                for observation in observations:
                    blob = observation.pop("polygon_blob")
                    observation["polygon"] = decode_polygon(blob) if blob else None
                value["observations"] = observations
                value["ocr_strings"] = list(dict.fromkeys(o["raw_text"] for o in observations))
                output.write(json.dumps(value, ensure_ascii=False) + "\n")
        temporary.replace(path)
        if rows:
            atomic_write(store.root / "rows.jsonl", path.read_text(encoding="utf-8"))
        else:
            (store.root / "rows.jsonl").unlink(missing_ok=True)
    else:
        (store.root / "rows.jsonl").unlink(missing_ok=True)
        (store.root / "text_clusters.jsonl").unlink(missing_ok=True)
    escape = lambda value: html.escape(str(value), quote=True)
    empty_message = ""
    if not hits:
        target = (
            store.rows("SELECT target_name FROM queries WHERE id=?", (query_id,))
            if query_id
            else []
        )
        if target:
            empty_message = (
                f"<p>No candidates for {escape(target[0]['target_name'])}. "
                "The scan may still contain other names. Run "
                "<code>tf2scan query --name NAME</code> to search saved text.</p>"
            )
        else:
            empty_message = "<p>No candidates in the selected report.</p>"
    body = []
    for hit in hits:
        link = hit["timestamp_url"]
        timestamp = f"{hit['start_s']:.1f}–{hit['end_s']:.1f}s"
        if link:
            timestamp = f'<a href="{escape(link)}" target="_blank" rel="noopener">{timestamp}</a>'
        crop = evidence_url(store, hit["evidence_path"])
        frame = evidence_url(store, hit["frame_path"])
        if crop:
            image = f'<img loading="lazy" src="{escape(crop)}" alt="Detected text crop">'
            evidence = f'<a href="{escape(frame)}">{image}</a>' if frame else image
        elif frame:
            evidence = f'<a href="{escape(frame)}">View sampled frame</a>'
        else:
            evidence = "Evidence unavailable"
        if crop or frame:
            evidence += f"<br><small>{escape(hit['evidence_kind'])} evidence"
            if hit["evidence_timestamp_s"] is not None:
                evidence += f" at {hit['evidence_timestamp_s']:.1f}s"
            evidence += f": {escape(hit['evidence_text'])}</small>"
        if hit["matched_observation_id"] and not hit["observation_evidence_available"]:
            evidence += "<br><small>Matching observation crop unavailable</small>"
        body.append(
            f"<tr><td>{hit['id']}</td><td>{escape(hit['target_name'])}</td>"
            f"<td>{escape(hit['title'])}</td><td data-value='{hit['start_s']}'>{timestamp}</td>"
            f"<td>{hit['best_score']:.3f}</td><td>{hit['support_count']}</td>"
            f"<td>{escape(hit['best_text'])}<br>{evidence}</td>"
            f"<td>{escape(hit['review_status'])}<br>{escape(hit['reviewer_note'])}</td></tr>"
        )
    document = (
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TF2 scan candidates</title>
<style>body{font:16px system-ui;margin:2rem;background:#141821;color:#edf0f7}a{color:#8ecbff}
table{border-collapse:collapse;width:100%}td,th{padding:.7rem;text-align:left;border-bottom:1px solid #465}
button{font:inherit;cursor:pointer}img{max-width:480px;width:100%}p{color:#bac7d7}</style>
<h1>TF2 scan candidates</h1><p>Click a column to sort. Images show the labelled detected text observation.
Review with <code>tf2scan review HIT_ID confirmed --note "..."</code>, then regenerate this report.</p>
"""
        + empty_message
        + """<table><thead><tr>"""
        + "".join(
            f'<th><button onclick="sortRows({i})">{label}</button></th>'
            for i, label in enumerate(
                ("Hit", "Target", "Video", "Time", "Score", "Frames", "Evidence", "Review")
            )
        )
        + """</tr></thead>
<tbody>"""
        + "".join(body)
        + """</tbody></table><script>
let direction=1,last=-1;function sortRows(n){direction=last===n?-direction:1;last=n;
const body=document.querySelector('tbody');const value=r=>r.cells[n].dataset.value||r.cells[n].textContent;
[...body.rows].sort((a,b)=>{let x=value(a),y=value(b);return direction*(
x.trim()!==''&&y.trim()!==''&&Number.isFinite(Number(x))&&Number.isFinite(Number(y))
?Number(x)-Number(y):x.localeCompare(y));}).forEach(r=>body.appendChild(r));}
</script></html>"""
    )
    atomic_write(store.root / "report/index.html", document)
    store.prune_orphan_evidence()
    return hits
