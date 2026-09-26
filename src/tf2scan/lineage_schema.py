"""Additive v3 migration. Original tables remain available for recovery/audit."""

SCHEMA = r"""
ALTER TABLE observations RENAME TO legacy_observations;
ALTER TABLE row_clusters RENAME TO legacy_row_clusters;
ALTER TABLE hits RENAME TO legacy_hits;
ALTER TABLE query_matches RENAME TO legacy_query_matches;
ALTER TABLE queries ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}';
CREATE TABLE scan_runs (
 id INTEGER PRIMARY KEY, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
 completed_at TEXT, status TEXT NOT NULL DEFAULT 'running'
 CHECK(status IN ('running','completed','failed')), legacy INTEGER NOT NULL DEFAULT 0,
 config_hash TEXT NOT NULL, config_json TEXT NOT NULL,
 detector_name TEXT, detector_version TEXT, detector_weights_hash TEXT,
 recognizer_name TEXT, recognizer_version TEXT, recognizer_weights_hash TEXT,
 preprocessing_version TEXT, normalization_version TEXT, geometry_encoding_version TEXT,
 software_versions_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE video_scans (
 id INTEGER PRIMARY KEY, scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
 video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
 error TEXT, sample_fps REAL, input_width INTEGER, input_height INTEGER,
 started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
 UNIQUE(scan_run_id,video_id)
);
CREATE TABLE scan_chunks (
 id INTEGER PRIMARY KEY, video_scan_id INTEGER NOT NULL REFERENCES video_scans(id) ON DELETE CASCADE,
 sequence_no INTEGER NOT NULL CHECK(sequence_no>=0),
 status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
 error TEXT, chunk_start_s REAL NOT NULL CHECK(chunk_start_s>=0), chunk_end_s REAL,
 last_processed_timestamp_s REAL, download_mode TEXT NOT NULL,
 started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
 CHECK(chunk_end_s IS NULL OR chunk_end_s>chunk_start_s), UNIQUE(video_scan_id,sequence_no),
 UNIQUE(id,video_scan_id)
);
CREATE TABLE chunk_attempts (
 id INTEGER PRIMARY KEY, scan_chunk_id INTEGER NOT NULL REFERENCES scan_chunks(id) ON DELETE CASCADE,
 attempt_no INTEGER NOT NULL CHECK(attempt_no>0), error TEXT,
 started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
 UNIQUE(scan_chunk_id,attempt_no)
);
CREATE TABLE sampled_frames (
 id INTEGER PRIMARY KEY, scan_chunk_id INTEGER NOT NULL,
 video_scan_id INTEGER NOT NULL REFERENCES video_scans(id) ON DELETE CASCADE,
 sample_key TEXT NOT NULL, timestamp_s REAL NOT NULL CHECK(timestamp_s>=0),
 source_width INTEGER, source_height INTEGER, working_width INTEGER, working_height INTEGER,
 raw_detection_count INTEGER CHECK(raw_detection_count>=0),
 duplicate_detection_count INTEGER CHECK(duplicate_detection_count>=0),
 recognition_count INTEGER CHECK(recognition_count>=0),
 accepted_observation_count INTEGER CHECK(accepted_observation_count>=0),
 cap_dropped_count INTEGER CHECK(cap_dropped_count>=0),
 full_frame_path TEXT,
 FOREIGN KEY(scan_chunk_id,video_scan_id) REFERENCES scan_chunks(id,video_scan_id) ON DELETE CASCADE,
 UNIQUE(video_scan_id,sample_key), UNIQUE(video_scan_id,timestamp_s), UNIQUE(id,video_scan_id)
);
CREATE TABLE observations (
 id INTEGER PRIMARY KEY, sampled_frame_id INTEGER NOT NULL,
 video_scan_id INTEGER NOT NULL REFERENCES video_scans(id) ON DELETE CASCADE,
 polygon_blob BLOB CHECK(polygon_blob IS NULL OR length(polygon_blob)=32), geometry_key TEXT NOT NULL,
 screen_region TEXT, normalized_width REAL CHECK(normalized_width>0 AND normalized_width<=1),
 normalized_height REAL CHECK(normalized_height>0 AND normalized_height<=1),
 detector_confidence REAL CHECK(detector_confidence BETWEEN 0 AND 1),
 raw_text TEXT NOT NULL, normalized_text TEXT NOT NULL, compact_text TEXT NOT NULL,
 ocr_confidence REAL CHECK(ocr_confidence BETWEEN 0 AND 1), crop_path TEXT,
 legacy_metadata_json TEXT, legacy_evidence_path TEXT,
 FOREIGN KEY(sampled_frame_id,video_scan_id) REFERENCES sampled_frames(id,video_scan_id) ON DELETE CASCADE,
 UNIQUE(sampled_frame_id,geometry_key), UNIQUE(id,video_scan_id)
);
CREATE TABLE text_clusters (
 id INTEGER PRIMARY KEY, video_scan_id INTEGER NOT NULL REFERENCES video_scans(id) ON DELETE CASCADE,
 video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
 start_s REAL NOT NULL, end_s REAL NOT NULL CHECK(end_s>=start_s),
 representative_observation_id INTEGER, representative_polygon_blob BLOB, screen_region TEXT,
 canonical_text TEXT NOT NULL, normalized_text TEXT NOT NULL, compact_text TEXT NOT NULL,
 best_confidence REAL, support_count INTEGER NOT NULL,
 motion_summary_json TEXT, close_reason TEXT,
 row_index INTEGER, hud_profile TEXT, hud_config_json TEXT, model_version TEXT,
 normalization_version TEXT, evidence_path TEXT, frame_path TEXT,
 evidence_timestamp_s REAL, evidence_row_index INTEGER,
 FOREIGN KEY(representative_observation_id,video_scan_id)
 REFERENCES observations(id,video_scan_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(id,video_scan_id)
);
CREATE TABLE cluster_observations (
 cluster_id INTEGER NOT NULL, observation_id INTEGER NOT NULL, video_scan_id INTEGER NOT NULL,
 support_score REAL NOT NULL CHECK(support_score BETWEEN 0 AND 1),
 PRIMARY KEY(cluster_id,observation_id),
 FOREIGN KEY(cluster_id,video_scan_id) REFERENCES text_clusters(id,video_scan_id) ON DELETE CASCADE,
 FOREIGN KEY(observation_id,video_scan_id) REFERENCES observations(id,video_scan_id) ON DELETE CASCADE
);
CREATE TABLE hits (
 id INTEGER PRIMARY KEY, text_cluster_id INTEGER NOT NULL REFERENCES text_clusters(id) ON DELETE CASCADE,
 query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
 matched_alias TEXT NOT NULL, best_text TEXT NOT NULL, best_score REAL NOT NULL,
 support_count INTEGER NOT NULL, evidence_path TEXT,
 review_status TEXT NOT NULL DEFAULT 'unreviewed'
 CHECK(review_status IN ('unreviewed','confirmed','rejected')), reviewer_note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(text_cluster_id,query_id)
);
CREATE TABLE query_matches (
 query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
 observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
 matched_alias TEXT NOT NULL, match_score REAL NOT NULL,
 PRIMARY KEY(query_id,observation_id,matched_alias)
);
CREATE INDEX video_scans_video_status ON video_scans(video_id,status);
CREATE INDEX chunks_scan_status ON scan_chunks(video_scan_id,status);
CREATE INDEX frames_chunk_time ON sampled_frames(scan_chunk_id,timestamp_s);
CREATE INDEX observations_frame ON observations(sampled_frame_id);
CREATE INDEX text_clusters_scan_text ON text_clusters(video_scan_id,normalized_text);
CREATE INDEX cluster_support ON cluster_observations(cluster_id,observation_id);
CREATE TRIGGER immutable_run BEFORE UPDATE OF config_hash,config_json,legacy,
 detector_name,detector_version,detector_weights_hash,recognizer_name,recognizer_version,
 recognizer_weights_hash,preprocessing_version,normalization_version,geometry_encoding_version,
 software_versions_json,started_at ON scan_runs BEGIN
 SELECT RAISE(ABORT,'run provenance is immutable'); END;
CREATE TRIGGER immutable_scan_owner BEFORE UPDATE OF scan_run_id,video_id ON video_scans BEGIN
 SELECT RAISE(ABORT,'scan ownership is immutable'); END;
CREATE TRIGGER cluster_source_insert BEFORE INSERT ON text_clusters
 WHEN NEW.video_id != (SELECT video_id FROM video_scans WHERE id=NEW.video_scan_id) BEGIN
 SELECT RAISE(ABORT,'cluster source differs from scan'); END;
CREATE TRIGGER cluster_source_update BEFORE UPDATE OF video_id,video_scan_id ON text_clusters
 WHEN NEW.video_id != (SELECT video_id FROM video_scans WHERE id=NEW.video_scan_id) BEGIN
 SELECT RAISE(ABORT,'cluster source differs from scan'); END;
CREATE TRIGGER detection_observation_insert BEFORE INSERT ON observations
 WHEN (SELECT r.legacy FROM video_scans s JOIN scan_runs r ON r.id=s.scan_run_id
       WHERE s.id=NEW.video_scan_id)=0 AND
 (NEW.polygon_blob IS NULL OR NEW.screen_region IS NULL OR NEW.detector_confidence IS NULL
 OR NEW.normalized_width IS NULL OR NEW.normalized_height IS NULL) BEGIN
 SELECT RAISE(ABORT,'detector observation requires geometry'); END;
CREATE TRIGGER detection_frame_insert BEFORE INSERT ON sampled_frames
 WHEN (SELECT r.legacy FROM video_scans s JOIN scan_runs r ON r.id=s.scan_run_id
       WHERE s.id=NEW.video_scan_id)=0 AND
 (NEW.source_width IS NULL OR NEW.source_width<=0 OR NEW.source_height IS NULL OR NEW.source_height<=0
 OR NEW.working_width IS NULL OR NEW.working_width<=0 OR NEW.working_height IS NULL OR NEW.working_height<=0
 OR NEW.raw_detection_count IS NULL OR NEW.duplicate_detection_count IS NULL
 OR NEW.recognition_count IS NULL OR NEW.accepted_observation_count IS NULL OR NEW.cap_dropped_count IS NULL) BEGIN
 SELECT RAISE(ABORT,'detector frame requires dimensions and counters'); END;
CREATE TRIGGER detection_observation_update BEFORE UPDATE ON observations
 WHEN (SELECT r.legacy FROM video_scans s JOIN scan_runs r ON r.id=s.scan_run_id
       WHERE s.id=NEW.video_scan_id)=0 AND
 (NEW.polygon_blob IS NULL OR NEW.screen_region IS NULL OR NEW.detector_confidence IS NULL
 OR NEW.normalized_width IS NULL OR NEW.normalized_height IS NULL) BEGIN
 SELECT RAISE(ABORT,'detector observation requires geometry'); END;
CREATE TRIGGER observation_geometry_insert BEFORE INSERT ON observations
 WHEN NEW.polygon_blob IS NOT NULL AND valid_polygon(NEW.polygon_blob)!=1 BEGIN
 SELECT RAISE(ABORT,'invalid geometry'); END;
CREATE TRIGGER observation_geometry_update BEFORE UPDATE OF polygon_blob ON observations
 WHEN NEW.polygon_blob IS NOT NULL AND valid_polygon(NEW.polygon_blob)!=1 BEGIN
 SELECT RAISE(ABORT,'invalid geometry'); END;
CREATE TRIGGER representative_geometry_insert BEFORE INSERT ON text_clusters
 WHEN NEW.representative_polygon_blob IS NOT NULL AND valid_polygon(NEW.representative_polygon_blob)!=1 BEGIN
 SELECT RAISE(ABORT,'invalid representative geometry'); END;
CREATE TRIGGER representative_geometry_update BEFORE UPDATE OF representative_polygon_blob ON text_clusters
 WHEN NEW.representative_polygon_blob IS NOT NULL AND valid_polygon(NEW.representative_polygon_blob)!=1 BEGIN
 SELECT RAISE(ABORT,'invalid representative geometry'); END;
CREATE VIEW selected_video_scans AS
 SELECT s.* FROM video_scans s WHERE s.status='completed' AND NOT EXISTS
 (SELECT 1 FROM video_scans newer WHERE newer.video_id=s.video_id
 AND newer.status='completed' AND newer.id>s.id);
"""
