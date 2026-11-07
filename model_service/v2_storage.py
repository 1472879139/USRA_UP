"""v2 使用独立数据库，复用身份、幂等、取消和产物校验；不迁移 v1 表。"""
import json
from pathlib import Path
import uuid

from .contract import APIError, canonical, sha256
from .storage import Store, now


class V2Store(Store):
    artifact_api_prefix = "/v2"
    min_artifacts = 0

    def __init__(self, root, quota_root, max_bytes):
        self.quota_root = Path(quota_root)
        super().__init__(root, max_bytes)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS input_media(input_id TEXT PRIMARY KEY, media_type TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contracts(job_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
        """)
        self.db.commit()

    def input_path(self, identity):
        return self.root / "inputs" / (identity + ".bin")

    def used_bytes(self):
        return sum(p.stat().st_size for p in self.quota_root.rglob("*") if p.is_file() and not p.is_symlink())

    def add_file(self, owner, content, media_type):
        with self.lock:
            count = self.db.execute("SELECT count(*) FROM inputs WHERE owner=?", (owner,)).fetchone()[0]
            if count >= 128 or self.used_bytes() + len(content) > self.max_bytes:
                raise APIError(429, "STORAGE_LIMIT", "上传或存储配额已满")
            identity = uuid.uuid4().hex
            path = self.input_path(identity)
            value = {"file_id": identity, "sha256": sha256(content), "size_bytes": len(content),
                     "media_type": media_type, "created_at": now()}
            with path.open("xb") as stream:
                stream.write(content)
            try:
                with self.db:
                    self.db.execute("INSERT INTO inputs VALUES(?,?,?,?,?)",
                        (identity, owner, value["sha256"], len(content), value["created_at"]))
                    self.db.execute("INSERT INTO input_media VALUES(?,?)", (identity, media_type))
            except Exception:
                path.unlink()
                raise
            return value

    def file(self, owner, identity):
        with self.lock:
            row = self.input(owner, identity)
            media = self.db.execute("SELECT media_type FROM input_media WHERE input_id=?", (identity,)).fetchone()
            if media is None:
                raise APIError(404, "NOT_FOUND", "未找到本身份可用文件")
            return {**row, "media_type": media["media_type"]}

    def submit(self, owner, payload, idem, snapshot):
        with self.lock:
            previous = self.existing_submission(owner, payload, idem)
            if previous is not None:
                return previous, False
            active = self.db.execute("SELECT count(*) FROM jobs WHERE owner=? AND status IN ('queued','running','cancelling')", (owner,)).fetchone()[0]
            queued = self.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
            if active >= 4 or queued >= 16 or self.used_bytes() >= self.max_bytes:
                raise APIError(429, "QUEUE_FULL", "v2 任务或存储配额已满", True)
            identity, encoded = uuid.uuid4().hex, canonical(payload)
            with self.db:
                self.db.execute("INSERT INTO jobs(id,owner,input_id,capability_id,payload,payload_sha,idem,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
                    (identity, owner, None, payload["tool_id"], encoded, sha256(encoded.encode("utf-8")), idem, "queued", now()))
                self.db.execute("INSERT INTO contracts VALUES(?,?)", (identity, canonical(snapshot)))
            return self.public_job(owner, identity), True

    def public_job(self, owner, identity):
        with self.lock:
            row = self.db.execute("SELECT jobs.*,contracts.snapshot FROM jobs JOIN contracts ON contracts.job_id=jobs.id WHERE id=? AND owner=?",
                                  (identity, owner)).fetchone()
            if row is None:
                raise APIError(404, "NOT_FOUND", "未找到本身份 v2 任务")
            snapshot = json.loads(row["snapshot"])
            return {"api_version": "2.0.0", "job_id": identity, "tool_id": row["capability_id"],
                    "tool_version": snapshot["tool_version"], "contract_revision": snapshot["contract_revision"],
                    "status": row["status"], "created_at": row["created"], "started_at": row["started"],
                    "finished_at": row["finished"], "error": json.loads(row["error"]) if row["error"] else None,
                    "result": json.loads(row["result"]) if row["result"] else None}

    def claim(self):
        with self.lock:
            row = self.db.execute("SELECT jobs.*,contracts.snapshot FROM jobs JOIN contracts ON contracts.job_id=jobs.id WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET status='running',started=? WHERE id=?", (now(), row["id"]))
            self.db.commit()
            return {**dict(row), "payload": json.loads(row["payload"]), "snapshot": json.loads(row["snapshot"]), "_v2": True}
