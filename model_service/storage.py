"""持久任务与产物登记；API只通过身份和ID访问，不接受宿主路径。"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .contract import APIError, TERMINAL, canonical, sha256


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root, max_bytes=4 * 1024 ** 3):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        for name in ["inputs", "jobs"]:
            path = self.root / name
            if path.is_symlink():
                raise ValueError("数据目录不允许符号链接")
            path.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY, owner TEXT, sha TEXT, size INTEGER, created TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, owner TEXT, input_id TEXT, capability_id TEXT,
                payload TEXT, payload_sha TEXT, idem TEXT, status TEXT, created TEXT, started TEXT,
                finished TEXT, error TEXT, result TEXT, UNIQUE(owner,idem));
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, job_id TEXT, owner TEXT,
                name TEXT, sha TEXT, size INTEGER, media_type TEXT, role TEXT);
        """)
        error = canonical({"code": "SERVER_RESTARTED", "message": "服务重启中断了该次运行，请显式提交新任务", "retryable": True})
        self.db.execute("UPDATE jobs SET status='failed', finished=?, error=? WHERE status IN ('running','cancelling')", (now(), error))
        self.db.commit()

    def input_path(self, identity):
        return self.root / "inputs" / (identity + ".tif")

    def job_dir(self, identity):
        return self.root / "jobs" / identity

    def used_bytes(self):
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file() and not p.is_symlink())

    def add_input(self, owner, content):
        with self.lock:
            count = self.db.execute("SELECT count(*) FROM inputs WHERE owner=?", (owner,)).fetchone()[0]
            if count >= 128 or self.used_bytes() + len(content) > self.max_bytes:
                raise APIError(429, "STORAGE_LIMIT", "已达到服务存储配额，请联系维护者归档；未覆盖已有数据")
            identity = uuid.uuid4().hex
            path = self.input_path(identity)
            with path.open("xb") as stream:
                stream.write(content)
            value = {"input_id": identity, "sha256": sha256(content), "size_bytes": len(content), "created_at": now()}
            self.db.execute("INSERT INTO inputs VALUES(?,?,?,?,?)", (identity, owner, value["sha256"], len(content), value["created_at"]))
            self.db.commit()
            return value

    def input(self, owner, identity):
        row = self.db.execute("SELECT * FROM inputs WHERE id=? AND owner=?", (identity, owner)).fetchone()
        if not row:
            raise APIError(404, "NOT_FOUND", "未找到本身份可访问的输入")
        return dict(row)

    def submit(self, owner, payload, idem):
        with self.lock:
            encoded = canonical(payload)
            previous = self.db.execute("SELECT * FROM jobs WHERE owner=? AND idem=?", (owner, idem)).fetchone()
            if previous:
                if previous["payload_sha"] != sha256(encoded.encode("utf-8")):
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "同一个幂等标识不能用于不同任务")
                return self.public_job(owner, previous["id"]), False
            self.input(owner, payload["input_id"])
            active = self.db.execute("SELECT count(*) FROM jobs WHERE owner=? AND status IN ('queued','running','cancelling')", (owner,)).fetchone()[0]
            total = self.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
            if active >= 4 or total >= 16 or self.used_bytes() >= self.max_bytes:
                raise APIError(429, "QUEUE_FULL", "任务或存储配额已满，请稍后重试", True)
            identity = uuid.uuid4().hex
            self.db.execute("INSERT INTO jobs(id,owner,input_id,capability_id,payload,payload_sha,idem,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
                            (identity, owner, payload["input_id"], payload["capability_id"], encoded,
                             sha256(encoded.encode("utf-8")), idem, "queued", now()))
            self.db.commit()
            return self.public_job(owner, identity), True

    def public_job(self, owner, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (identity, owner)).fetchone()
            if not row:
                raise APIError(404, "NOT_FOUND", "未找到本身份可访问的任务")
            return {"job_id": row["id"], "capability_id": row["capability_id"], "input_id": row["input_id"],
                    "status": row["status"], "created_at": row["created"], "started_at": row["started"],
                    "finished_at": row["finished"], "error": json.loads(row["error"]) if row["error"] else None,
                    "result": json.loads(row["result"]) if row["result"] else None}

    def claim(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (now(), row["id"]))
            self.db.commit()
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            value["input"] = self.input(value["owner"], value["input_id"])
            return value

    def cancel(self, owner, identity):
        with self.lock:
            job = self.public_job(owner, identity)
            if job["status"] not in TERMINAL:
                status = "cancelled" if job["status"] == "queued" else "cancelling"
                self.db.execute("UPDATE jobs SET status=?,finished=? WHERE id=?", (status, now() if status == "cancelled" else None, identity))
                self.db.commit()
            return self.public_job(owner, identity)

    def fail(self, identity, code, message, retryable=False, cancelled=False):
        with self.lock:
            self.db.execute("UPDATE jobs SET status=?,finished=?,error=? WHERE id=?",
                            ("cancelled" if cancelled else "failed", now(), canonical({"code": code, "message": message, "retryable": retryable}), identity))
            self.db.commit()

    def succeed(self, task, result):
        with self.lock:
            state = self.db.execute("SELECT status FROM jobs WHERE id=?", (task["id"],)).fetchone()[0]
            if state == "cancelling":
                self.fail(task["id"], "JOB_CANCELLED", "任务已取消", cancelled=True)
                return
            if state != "running":
                raise ValueError("只有运行中的任务可以登记成功")
            job_root = self.job_dir(task["id"]).resolve()
            artifacts = []
            declared = result.pop("files")
            if not isinstance(declared, list) or not 1 <= len(declared) <= 16:
                raise ValueError("产物清单异常")
            total = 0
            for item in declared:
                name = item["name"]
                if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", name) or name in {".", ".."}:
                    raise ValueError("产物文件名异常")
                path = job_root / "output" / name
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(job_root):
                    raise ValueError("产物路径越界或文件缺失")
                size = path.stat().st_size
                total += size
                if total > 64 * 1024 ** 2:
                    raise ValueError("产物超过64MiB限制")
                identity, sha = uuid.uuid4().hex, sha256(path.read_bytes())
                if item.get("sha256") != sha or item.get("size_bytes") != size:
                    raise ValueError("产物哈希或体积不符")
                artifacts.append({"artifact_id": identity, "name": name, "sha256": sha, "size_bytes": size,
                                  "media_type": item["media_type"], "role": item["role"],
                                  "download_path": "/v1/artifacts/" + identity})
            # 先验证全部产物，再一次性登记，避免部分成功回执。
            result["artifacts"] = artifacts
            encoded_result = canonical(result)
            for item in artifacts:
                self.db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                                (item["artifact_id"], task["id"], task["owner"], item["name"], item["sha256"],
                                 item["size_bytes"], item["media_type"], item["role"]))
            self.db.execute("UPDATE jobs SET status='succeeded',finished=?,result=? WHERE id=?", (now(), encoded_result, task["id"]))
            self.db.commit()

    def artifact(self, owner, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM artifacts WHERE id=? AND owner=?", (identity, owner)).fetchone()
            if not row:
                raise APIError(404, "NOT_FOUND", "未找到本身份可访问的产物")
            path = self.job_dir(row["job_id"]) / "output" / row["name"]
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.root / "jobs"):
                raise APIError(410, "ARTIFACT_UNAVAILABLE", "产物已不在受控存储中")
            content = path.read_bytes()
            if len(content) != row["size"] or sha256(content) != row["sha"]:
                raise APIError(409, "ARTIFACT_CHANGED", "产物核验失败，已拒绝下载")
            return content, row["media_type"], row["name"]

    def close(self):
        self.db.close()
