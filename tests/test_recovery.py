import sqlite3

from tf2scan.storage import MIGRATIONS, Store


def test_migration_preserves_existing_corpus(tmp_path):
    db = sqlite3.connect(tmp_path / "results.sqlite3")
    db.executescript(MIGRATIONS[0])
    db.execute("PRAGMA user_version=1")
    db.execute("INSERT INTO videos(id,source_url,title) VALUES ('video','url','title')")
    db.commit()
    db.close()
    with Store(tmp_path) as store:
        assert store.video("video")["title"] == "title"
        assert store.video("video")["scan_config_json"] is None
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_orphan_cleanup_only_removes_owned_names(tmp_path):
    folder = tmp_path / "report/assets" / ("a" * 16 + "-" + "b" * 32)
    folder.mkdir(parents=True)
    orphan = folder / "1-row.png"
    orphan.write_bytes(b"orphan")
    unrelated = folder / "notes.txt"
    unrelated.write_text("keep")
    with Store(tmp_path) as store:
        store.prune_orphan_evidence()
        assert not orphan.exists()
        assert unrelated.exists()
