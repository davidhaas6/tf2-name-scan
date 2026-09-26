import html
import json
import math
from pathlib import Path
from urllib.parse import urlsplit


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


def export_report(store, query_id=None, rows=False, *, scan_id=None, run_id=None):
    condition, params = ("WHERE h.query_id=?", (query_id,)) if query_id else ("", ())
    scan_ids = store.selected_scan_ids(scan_id=scan_id, run_id=run_id)
    condition += (
        (" AND " if condition else "WHERE ")
        + "c.video_scan_id IN ("
        + ",".join("?" for _ in scan_ids)
        + ")"
    )
    params = (*params, *scan_ids)
    hits = store.rows(
        f"""SELECT h.*,c.video_id,c.start_s,c.end_s,c.frame_path,
        c.model_version,c.normalization_version,c.hud_profile,c.evidence_timestamp_s,
        c.evidence_row_index,v.title,v.source_url,
        q.target_name FROM hits h JOIN text_clusters c ON c.id=h.text_cluster_id
        JOIN videos v ON v.id=c.video_id JOIN queries q ON q.id=h.query_id
        {condition} ORDER BY h.best_score DESC,c.start_s""",
        params,
    )
    for hit in hits:
        hit["timestamp_url"] = timestamp_url(hit["source_url"], hit["start_s"])
        hit["ocr_strings"] = [
            row[0]
            for row in store.db.execute(
                "SELECT DISTINCT raw_text FROM observations o JOIN cluster_observations co "
                "ON co.observation_id=o.id WHERE co.cluster_id=?",
                (hit["text_cluster_id"],),
            )
        ]
    atomic_write(
        store.root / "hits.jsonl", "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in hits)
    )
    if rows:
        path = store.root / "rows.jsonl"
        temporary = path.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            for row in store.selected_clusters(scan_id=scan_id, run_id=run_id):
                value = dict(row)
                video = store.video(row["video_id"])
                value.update({key: video[key] for key in ("source_url", "title", "channel_id")})
                if value["representative_polygon_blob"] is not None:
                    from .geometry import decode_polygon

                    value["representative_polygon"] = decode_polygon(
                        value["representative_polygon_blob"]
                    )
                del value["representative_polygon_blob"]
                value["ocr_strings"] = [
                    r[0]
                    for r in store.db.execute(
                        "SELECT DISTINCT raw_text FROM observations o JOIN cluster_observations co "
                        "ON co.observation_id=o.id WHERE co.cluster_id=?",
                        (row["id"],),
                    )
                ]
                output.write(json.dumps(value, ensure_ascii=False) + "\n")
        temporary.replace(path)
    escape = lambda value: html.escape(str(value), quote=True)
    body = []
    for hit in hits:
        link = hit["timestamp_url"]
        timestamp = f"{hit['start_s']:.1f}–{hit['end_s']:.1f}s"
        if link:
            timestamp = f'<a href="{escape(link)}" target="_blank" rel="noopener">{timestamp}</a>'
        crop = (
            Path(hit["evidence_path"]).relative_to("report").as_posix()
            if hit["evidence_path"]
            else ""
        )
        frame = (
            Path(hit["frame_path"]).relative_to("report").as_posix() if hit["frame_path"] else ""
        )
        evidence = f'<a href="{escape(frame)}"><img loading="lazy" src="{escape(crop)}" alt="Killfeed row"></a>'
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
<h1>TF2 scan candidates</h1><p>Click a column to sort. Click a crop for its representative frame.
Review with <code>tf2scan review HIT_ID confirmed --note "..."</code>, then regenerate this report.</p>
<table><thead><tr>"""
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
