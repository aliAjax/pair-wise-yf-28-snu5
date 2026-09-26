"""临床试验分层区组随机分配与盲法服务。"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "randomization.db"
MAX_ARM_LENGTH = 40


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RandomizationStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('site','coordinator','monitor')),
                    site_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS trials(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                    protocol_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','running','stopped')),
                    arms_json TEXT NOT NULL, strata_factors_json TEXT NOT NULL,
                    block_size INTEGER NOT NULL CHECK(block_size >= 2),
                    seed TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, started_at TEXT
                );
                CREATE TABLE IF NOT EXISTS strata(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    revision_no INTEGER NOT NULL DEFAULT 0,
                    stratum_key TEXT NOT NULL, factors_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(trial_id,revision_no,stratum_key)
                );
                CREATE TABLE IF NOT EXISTS allocations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    sequence INTEGER NOT NULL, block_no INTEGER NOT NULL,
                    arm TEXT NOT NULL, used_by INTEGER, used_at TEXT,
                    UNIQUE(stratum_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS participants(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    site_id TEXT NOT NULL, external_id TEXT NOT NULL,
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    allocation_id INTEGER NOT NULL UNIQUE REFERENCES allocations(id),
                    allocation_code TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'enrolled'
                        CHECK(status IN ('enrolled','withdrawn','completed')),
                    enrolled_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(trial_id,external_id)
                );
                CREATE TABLE IF NOT EXISTS unblinding_requests(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    participant_id INTEGER NOT NULL REFERENCES participants(id),
                    requester_id TEXT NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                    first_approver TEXT REFERENCES users(id), second_approver TEXT REFERENCES users(id),
                    decided_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id INTEGER REFERENCES trials(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS protocol_amendments(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    revision_no INTEGER NOT NULL,
                    protocol_version TEXT NOT NULL,
                    arms_json TEXT NOT NULL, strata_factors_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','effective','rejected')),
                    submitted_by TEXT NOT NULL REFERENCES users(id), submitted_at TEXT NOT NULL,
                    first_confirmer TEXT REFERENCES users(id), first_confirmed_at TEXT,
                    second_confirmer TEXT REFERENCES users(id), second_confirmed_at TEXT,
                    rejected_by TEXT REFERENCES users(id), rejected_at TEXT, reject_reason TEXT,
                    effective_at TEXT,
                    UNIQUE(trial_id,revision_no)
                );
                """
            )
            self._migrate(conn)

    def _migrate(self, conn):
        """为旧库补齐方案修订所需列与唯一约束。"""
        conn.commit()  # executescript 已提交；确保 PRAGMA 不在事务中
        trial_cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
        if "revision_no" not in trial_cols:
            conn.execute("ALTER TABLE trials ADD COLUMN revision_no INTEGER NOT NULL DEFAULT 0")
        alloc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(allocations)")}
        if "revision_no" not in alloc_cols:
            conn.execute("ALTER TABLE allocations ADD COLUMN revision_no INTEGER NOT NULL DEFAULT 0")
        strata_cols = {r["name"] for r in conn.execute("PRAGMA table_info(strata)")}
        if "revision_no" not in strata_cols:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "CREATE TABLE strata_new(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "trial_id INTEGER NOT NULL REFERENCES trials(id),revision_no INTEGER NOT NULL DEFAULT 0,"
                "stratum_key TEXT NOT NULL,factors_json TEXT NOT NULL,created_at TEXT NOT NULL,"
                "UNIQUE(trial_id,revision_no,stratum_key))"
            )
            conn.execute(
                "INSERT INTO strata_new(id,trial_id,revision_no,stratum_key,factors_json,created_at)"
                " SELECT id,trial_id,0,stratum_key,factors_json,created_at FROM strata"
            )
            conn.execute("DROP TABLE strata")
            conn.execute("ALTER TABLE strata_new RENAME TO strata")
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_strata_trial_rev_key "
            "ON strata(trial_id,revision_no,stratum_key)"
        )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,site_id) VALUES(?,?,?,?)",
                [
                    ("site1", "中心一协调员", "site", "S001"),
                    ("site2", "中心二协调员", "site", "S002"),
                    ("coord", "项目协调员", "coordinator", "CENTER"),
                    ("monitor1", "独立监查员甲", "monitor", "CENTER"),
                    ("monitor2", "独立监查员乙", "monitor", "CENTER"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _trial(self, conn, trial_id):
        row = conn.execute("SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
        if not row:
            raise BusinessError("试验不存在", 404, "not_found")
        return row

    def _audit(self, conn, trial_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(trial_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (trial_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def create_trial(self, user_id, name, protocol_version, arms, strata_factors, block_size, seed):
        name = name.strip()
        if len(name) < 3 or not protocol_version.strip() or len(seed.strip()) < 8:
            raise BusinessError("试验名称、方案版本和至少 8 位随机种子不能为空", 422, "invalid_trial")
        if not isinstance(arms, list) or len(arms) < 2:
            raise BusinessError("至少需要两个试验组", 422, "invalid_arms")
        arms = [str(a).strip() for a in arms]
        if any(not a or len(a) > MAX_ARM_LENGTH for a in arms) or len(set(arms)) != len(arms):
            raise BusinessError("试验组名称必须非空、唯一且不过长", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or any(not str(x).strip() for x in strata_factors) or len(set(strata_factors)) != len(strata_factors):
            raise BusinessError("分层因素必须是非空且不重复的数组", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms) != 0:
            raise BusinessError("区组长度必须为试验组数的正整数倍", 422, "invalid_block_size")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"coordinator"})
            try:
                cur = conn.execute(
                    """INSERT INTO trials(name,protocol_version,arms_json,strata_factors_json,block_size,seed,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (name, protocol_version.strip(), json.dumps(arms), json.dumps([str(x).strip() for x in strata_factors]), block_size, seed.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("试验名称已存在", 409, "trial_exists")
            trial_id = cur.lastrowid
            self._audit(conn, trial_id, user_id, "trial.create", {"protocol_version": protocol_version, "arms": len(arms), "block_size": block_size})
            return {"id": trial_id, "name": name, "status": "draft", "arms": arms, "strata_factors": strata_factors, "block_size": block_size}

    def update_protocol(self, user_id, trial_id, protocol_version, arms=None, strata_factors=None, block_size=None, seed=None):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            enrolled = conn.execute("SELECT COUNT(*) FROM participants WHERE trial_id=?", (trial_id,)).fetchone()[0]
            if enrolled or trial["status"] != "draft":
                raise BusinessError("入组开始后不能修改随机方案", 409, "protocol_locked")
            new_arms = arms if arms is not None else json.loads(trial["arms_json"])
            new_strata = strata_factors if strata_factors is not None else json.loads(trial["strata_factors_json"])
            new_block = block_size if block_size is not None else trial["block_size"]
            new_seed = str(seed) if seed is not None else trial["seed"]
            self.create_trial_validation_only(new_arms, new_strata, new_block, new_seed)
            conn.execute(
                """UPDATE trials SET protocol_version=?,arms_json=?,strata_factors_json=?,block_size=?,seed=? WHERE id=?""",
                (protocol_version.strip(), json.dumps(new_arms), json.dumps(new_strata), new_block, new_seed, trial_id),
            )
            self._audit(conn, trial_id, user_id, "protocol.update", {"protocol_version": protocol_version})
            return {"id": trial_id, "protocol_version": protocol_version, "arms": new_arms, "block_size": new_block}

    @staticmethod
    def create_trial_validation_only(arms, strata_factors, block_size, seed):
        if not isinstance(arms, list) or len(arms) < 2 or len(set(arms)) != len(arms):
            raise BusinessError("试验组配置无效", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or not strata_factors or len(set(strata_factors)) != len(strata_factors):
            raise BusinessError("分层因素配置无效", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms):
            raise BusinessError("区组长度无效", 422, "invalid_block_size")
        if len(str(seed)) < 8:
            raise BusinessError("随机种子至少 8 位", 422, "invalid_seed")

    def start_trial(self, user_id, trial_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            if trial["status"] != "draft":
                raise BusinessError("只有草稿试验可以开始", 409, "invalid_status")
            conn.execute("UPDATE trials SET status='running',started_at=? WHERE id=?", (now(), trial_id))
            self._audit(conn, trial_id, user_id, "trial.start", {})
            return {"id": trial_id, "status": "running"}

    def _stratum(self, conn, trial, factors, site_id):
        expected = json.loads(trial["strata_factors_json"])
        if set(factors) != set(expected):
            raise BusinessError(f"必须提供分层因素: {', '.join(expected)}", 422, "invalid_factors")
        normalized = {k: str(factors[k]).strip() for k in sorted(expected)}
        if any(not v for v in normalized.values()):
            raise BusinessError("分层因素值不能为空", 422, "invalid_factors")
        revision = trial["revision_no"]
        key = f"{site_id}|" + json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = conn.execute(
            "SELECT * FROM strata WHERE trial_id=? AND revision_no=? AND stratum_key=?",
            (trial["id"], revision, key),
        ).fetchone()
        if row:
            return row
        cur = conn.execute(
            "INSERT INTO strata(trial_id,revision_no,stratum_key,factors_json,created_at) VALUES(?,?,?,?,?)",
            (trial["id"], revision, key, json.dumps({"site_id": site_id, **normalized}, ensure_ascii=False, sort_keys=True), now()),
        )
        return conn.execute("SELECT * FROM strata WHERE id=?", (cur.lastrowid,)).fetchone()

    def _next_allocation(self, conn, trial, stratum):
        for block_no in range(1, 101):
            count = conn.execute(
                "SELECT COUNT(*) FROM allocations WHERE stratum_id=? AND block_no=?", (stratum["id"], block_no)
            ).fetchone()[0]
            if count == 0:
                # 修订版本号进入随机种子：新规则生成的随机序列与旧规则完全隔离，
                # 早先为旧规则预留但未使用的随机号不会再被发给新规则下的受试者。
                # 修订 0 沿用原始种子格式，保证修订功能上线前已生成的随机表逐位一致。
                rev_tag = "" if trial["revision_no"] == 0 else f":r{trial['revision_no']}"
                rng = random.Random(f"{trial['seed']}{rev_tag}:{stratum['stratum_key']}:{block_no}")
                arms = json.loads(trial["arms_json"])
                plan = []
                blocks = len(arms) if trial["block_size"] > len(arms) else 1
                for _ in range(blocks * (trial["block_size"] // len(arms))):
                    plan.extend(arms)
                rng.shuffle(plan)
                start = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM allocations WHERE stratum_id=?", (stratum["id"],)
                ).fetchone()[0]
                for offset, arm in enumerate(plan, 1):
                    conn.execute(
                        "INSERT INTO allocations(trial_id,stratum_id,revision_no,sequence,block_no,arm) VALUES(?,?,?,?,?,?)",
                        (trial["id"], stratum["id"], trial["revision_no"], start + offset, block_no, arm),
                    )
            free = conn.execute(
                "SELECT * FROM allocations WHERE stratum_id=? AND used_by IS NULL ORDER BY sequence LIMIT 1", (stratum["id"],)
            ).fetchone()
            if free:
                return free
        raise BusinessError("随机分配表已耗尽，请由统计人员扩展方案", 409, "allocation_exhausted")

    def enroll(self, user_id, trial_id, external_id, factors):
        external_id = str(external_id).strip()
        if not external_id:
            raise BusinessError("外部受试者编号不能为空", 422, "invalid_external_id")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                trial = self._trial(conn, trial_id)
                if trial["status"] != "running":
                    raise BusinessError("试验尚未开始或已经停止", 409, "trial_not_running")
                pending = conn.execute(
                    "SELECT id FROM protocol_amendments WHERE trial_id=? AND status='pending'", (trial_id,)
                ).fetchone()
                if pending:
                    raise BusinessError(
                        f"方案修订(编号{pending['id']})等待双人确认，入组已暂停；生效前仍按旧规则执行",
                        409, "enrollment_paused_amendment_pending",
                    )
                existing = conn.execute(
                    "SELECT * FROM participants WHERE trial_id=? AND external_id=?", (trial_id, external_id)
                ).fetchone()
                if existing:
                    if existing["site_id"] != actor["site_id"]:
                        raise BusinessError("不能在当前中心查看其他中心的受试者", 403, "site_isolation")
                    conn.commit()
                    return self._blinded_participant(conn, existing, actor, allow_arm=False, idempotent=True)
                stratum = self._stratum(conn, trial, factors, actor["site_id"])
                allocation = self._next_allocation(conn, trial, stratum)
                allocation_code = hashlib.sha256(f"{trial_id}:{external_id}".encode()).hexdigest()[:12].upper()
                cur = conn.execute(
                    """INSERT INTO participants(trial_id,site_id,external_id,stratum_id,allocation_id,allocation_code,enrolled_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (trial_id, actor["site_id"], external_id, stratum["id"], allocation["id"], allocation_code, user_id, now()),
                )
                participant_id = cur.lastrowid
                conn.execute("UPDATE allocations SET used_by=?,used_at=? WHERE id=?", (participant_id, now(), allocation["id"]))
                self._audit(conn, trial_id, user_id, "participant.enroll", {"participant_id": participant_id, "external_id": external_id, "allocation_id": allocation["id"], "site_id": actor["site_id"], "revision_no": trial["revision_no"]})
                participant = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
                return self._blinded_participant(conn, participant, actor, allow_arm=False, idempotent=False)
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "participants.trial_id, participants.external_id" in str(exc):
                    with self.connect() as retry:
                        row = retry.execute("SELECT * FROM participants WHERE trial_id=? AND external_id=?", (trial_id, external_id)).fetchone()
                        if row and row["site_id"] == actor["site_id"]:
                            return self._blinded_participant(retry, row, actor, False, True)
                raise BusinessError("并发入组冲突，请重新提交", 409, "enrollment_conflict")
            except Exception:
                conn.rollback()
                raise

    def _blinded_participant(self, conn, participant, viewer, allow_arm=False, idempotent=False):
        result = {
            "id": participant["id"], "trial_id": participant["trial_id"],
            "external_id": participant["external_id"], "site_id": participant["site_id"],
            "allocation_code": participant["allocation_code"], "status": participant["status"],
            "created_at": participant["created_at"], "idempotent": idempotent,
        }
        if allow_arm:
            result["arm"] = conn.execute("SELECT arm FROM allocations WHERE id=?", (participant["allocation_id"],)).fetchone()["arm"]
        return result

    def list_participants(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            self._trial(conn, trial_id)
            if actor["role"] == "site":
                rows = conn.execute("SELECT * FROM participants WHERE trial_id=? AND site_id=? ORDER BY id", (trial_id, actor["site_id"])).fetchall()
            else:
                rows = conn.execute("SELECT * FROM participants WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()
            return [self._blinded_participant(conn, row, actor) for row in rows]

    def get_participant(self, user_id, participant_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            row = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
            if not row:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and row["site_id"] != actor["site_id"]:
                raise BusinessError("只能查看本中心受试者", 403, "site_isolation")
            approved = conn.execute(
                "SELECT 1 FROM unblinding_requests WHERE participant_id=? AND status='approved'", (participant_id,)
            ).fetchone() is not None
            return self._blinded_participant(conn, row, actor, allow_arm=approved)

    def request_unblinding(self, user_id, participant_id, reason):
        if len(reason.strip()) < 8:
            raise BusinessError("揭盲原因至少 8 字", 422, "reason_required")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator"})
            participant = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
            if not participant:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and participant["site_id"] != actor["site_id"]:
                raise BusinessError("不能申请其他中心的揭盲", 403, "site_isolation")
            open_request = conn.execute(
                "SELECT id FROM unblinding_requests WHERE participant_id=? AND status='pending'", (participant_id,)
            ).fetchone()
            if open_request:
                raise BusinessError("该受试者已有待审批的揭盲申请", 409, "request_exists")
            cur = conn.execute(
                "INSERT INTO unblinding_requests(participant_id,requester_id,reason,created_at) VALUES(?,?,?,?)",
                (participant_id, user_id, reason.strip(), now()),
            )
            self._audit(conn, participant["trial_id"], user_id, "unblinding.request", {"request_id": cur.lastrowid, "participant_id": participant_id})
            return {"id": cur.lastrowid, "status": "pending"}

    def approve_unblinding(self, user_id, request_id):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                approver = self._user(conn, user_id, {"monitor", "coordinator"})
                request = conn.execute("SELECT * FROM unblinding_requests WHERE id=?", (request_id,)).fetchone()
                if not request:
                    raise BusinessError("揭盲申请不存在", 404, "not_found")
                if request["status"] != "pending":
                    raise BusinessError("揭盲申请已经完成", 409, "already_decided")
                if request["first_approver"] is None:
                    conn.execute("UPDATE unblinding_requests SET first_approver=? WHERE id=?", (user_id, request_id))
                    participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
                    self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.first", {"request_id": request_id})
                    return {"id": request_id, "status": "pending", "first_approver": user_id, "second_approval_required": True}
                if request["first_approver"] == user_id:
                    raise BusinessError("两次揭盲审批必须由不同人员完成", 409, "distinct_approver_required")
                conn.execute(
                    "UPDATE unblinding_requests SET second_approver=?,status='approved',decided_at=? WHERE id=?",
                    (user_id, now(), request_id),
                )
                participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
                arm = conn.execute("SELECT arm FROM allocations WHERE id=?", (participant["allocation_id"],)).fetchone()["arm"]
                self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.second", {"request_id": request_id, "participant_id": participant["id"]})
                return {"id": request_id, "status": "approved", "first_approver": request["first_approver"], "second_approver": user_id, "arm": arm}
            except Exception:
                conn.rollback()
                raise

    def submit_amendment(self, user_id, trial_id, protocol_version, arms, strata_factors):
        protocol_version = str(protocol_version).strip()
        if not protocol_version:
            raise BusinessError("新方案版本号不能为空", 422, "protocol_version_required")
        if not isinstance(arms, list) or not isinstance(strata_factors, list):
            raise BusinessError("新分组和分层因素必须以数组提供", 422, "invalid_amendment")
        arms = [str(a).strip() for a in arms]
        strata_factors = [str(s).strip() for s in strata_factors]
        with self.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            if trial["status"] != "running":
                raise BusinessError("只有入组中的试验可以提交方案修订", 409, "invalid_status")
            existing = conn.execute(
                "SELECT id,status FROM protocol_amendments WHERE trial_id=?", (trial_id,)
            ).fetchall()
            if any(a["status"] == "pending" for a in existing):
                raise BusinessError("已有方案修订等待确认，不能重复提交", 409, "amendment_pending")
            if any(a["status"] == "effective" for a in existing):
                raise BusinessError("方案修订已经生效，不能再次修订", 409, "amendment_locked")
            old_arms = json.loads(trial["arms_json"])
            old_factors = json.loads(trial["strata_factors_json"])
            # 分组与分层规则一并校验；区组长度和种子保持不变，保证随机表生成机制一致。
            self.create_trial_validation_only(arms, strata_factors, trial["block_size"], trial["seed"])
            if arms == old_arms and strata_factors == old_factors:
                raise BusinessError("新分组和分层规则与现行方案一致，无需修订", 422, "amendment_no_change")
            revision_row = conn.execute(
                "SELECT COALESCE(MAX(revision_no),0) AS rev FROM protocol_amendments WHERE trial_id=?", (trial_id,)
            ).fetchone()
            revision_no = max(trial["revision_no"] or 0, revision_row["rev"]) + 1
            cur = conn.execute(
                """INSERT INTO protocol_amendments(trial_id,revision_no,protocol_version,arms_json,
                       strata_factors_json,submitted_by,submitted_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (trial_id, revision_no, protocol_version, json.dumps(arms, ensure_ascii=False),
                 json.dumps(strata_factors, ensure_ascii=False), user_id, now()),
            )
            self._audit(conn, trial_id, user_id, "amendment.submit", {
                "amendment_id": cur.lastrowid, "revision_no": revision_no,
                "protocol_version": protocol_version,
                "old": {"arms": old_arms, "strata_factors": old_factors},
                "new": {"arms": arms, "strata_factors": strata_factors},
            })
            saved = conn.execute("SELECT * FROM protocol_amendments WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._amendment_dict(saved, include_arms=True)

    def confirm_amendment(self, user_id, amendment_id):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"monitor"})
                row = conn.execute("SELECT * FROM protocol_amendments WHERE id=?", (amendment_id,)).fetchone()
                if not row:
                    raise BusinessError("方案修订不存在", 404, "not_found")
                trial = self._trial(conn, row["trial_id"])
                if trial["status"] != "running":
                    raise BusinessError("试验已停止，不能再确认修订", 409, "invalid_status")
                if row["status"] != "pending":
                    raise BusinessError("该修订已结束，不能重复确认", 409, "amendment_closed")
                if row["first_confirmer"] is None:
                    conn.execute(
                        "UPDATE protocol_amendments SET first_confirmer=?,first_confirmed_at=? WHERE id=?",
                        (user_id, now(), amendment_id),
                    )
                    self._audit(conn, row["trial_id"], user_id, "amendment.confirm.first",
                                {"amendment_id": amendment_id, "revision_no": row["revision_no"]})
                    conn.commit()
                    return self._amendment_dict(
                        conn.execute("SELECT * FROM protocol_amendments WHERE id=?", (amendment_id,)).fetchone())
                if row["first_confirmer"] == user_id:
                    raise BusinessError("两次确认必须由不同监查员完成", 409, "distinct_confirmer_required")
                effective_at = now()
                conn.execute(
                    """UPDATE protocol_amendments SET second_confirmer=?,second_confirmed_at=?,
                           status='effective',effective_at=? WHERE id=?""",
                    (user_id, effective_at, effective_at, amendment_id),
                )
                conn.execute(
                    """UPDATE trials SET revision_no=?,protocol_version=?,arms_json=?,strata_factors_json=? WHERE id=?""",
                    (row["revision_no"], row["protocol_version"], row["arms_json"],
                     row["strata_factors_json"], row["trial_id"]),
                )
                self._audit(conn, row["trial_id"], user_id, "amendment.confirm.second", {
                    "amendment_id": amendment_id, "revision_no": row["revision_no"],
                    "effective_at": effective_at,
                })
                self._audit(conn, row["trial_id"], user_id, "amendment.effective", {
                    "revision_no": row["revision_no"], "protocol_version": row["protocol_version"],
                    "arms": json.loads(row["arms_json"]),
                    "strata_factors": json.loads(row["strata_factors_json"]),
                })
                conn.commit()
                return self._amendment_dict(
                    conn.execute("SELECT * FROM protocol_amendments WHERE id=?", (amendment_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise

    def reject_amendment(self, user_id, amendment_id, reason):
        reason = str(reason or "").strip()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"monitor"})
                row = conn.execute("SELECT * FROM protocol_amendments WHERE id=?", (amendment_id,)).fetchone()
                if not row:
                    raise BusinessError("方案修订不存在", 404, "not_found")
                if row["status"] != "pending":
                    raise BusinessError("该修订已结束，不能驳回", 409, "amendment_closed")
                conn.execute(
                    "UPDATE protocol_amendments SET status='rejected',rejected_by=?,rejected_at=?,reject_reason=? WHERE id=?",
                    (user_id, now(), reason, amendment_id),
                )
                self._audit(conn, row["trial_id"], user_id, "amendment.reject",
                            {"amendment_id": amendment_id, "revision_no": row["revision_no"], "reason": reason})
                conn.commit()
                return self._amendment_dict(
                    conn.execute("SELECT * FROM protocol_amendments WHERE id=?", (amendment_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise

    def list_amendments(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            self._trial(conn, trial_id)
            rows = conn.execute(
                "SELECT * FROM protocol_amendments WHERE trial_id=? ORDER BY revision_no", (trial_id,)
            ).fetchall()
            # 分组（试验用药/器械臂）对中心用户保密；监查员与协调员可见完整修订内容。
            return [self._amendment_dict(r, include_arms=actor["role"] != "site") for r in rows]

    @staticmethod
    def _amendment_dict(row, include_arms=True):
        data = {
            "id": row["id"], "trial_id": row["trial_id"], "revision_no": row["revision_no"],
            "protocol_version": row["protocol_version"], "status": row["status"],
            "submitted_by": row["submitted_by"], "submitted_at": row["submitted_at"],
            "first_confirmer": row["first_confirmer"], "first_confirmed_at": row["first_confirmed_at"],
            "second_confirmer": row["second_confirmer"], "second_confirmed_at": row["second_confirmed_at"],
            "rejected_by": row["rejected_by"], "rejected_at": row["rejected_at"],
            "reject_reason": row["reject_reason"], "effective_at": row["effective_at"],
            "strata_factors": json.loads(row["strata_factors_json"]),
        }
        if include_arms:
            data["arms"] = json.loads(row["arms_json"])
        return data

    def trial_summary(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            trial = self._trial(conn, trial_id)
            where, params = "", [trial_id]
            if actor["role"] == "site":
                where, params = " AND site_id=?", [trial_id, actor["site_id"]]
            total = conn.execute(f"SELECT COUNT(*) FROM participants WHERE trial_id=?" + where, params).fetchone()[0]
            by_site = conn.execute(
                f"SELECT site_id,COUNT(*) AS count FROM participants WHERE trial_id=?" + where + " GROUP BY site_id", params
            ).fetchall()
            audit = conn.execute("SELECT * FROM audit_log WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()
            pending = conn.execute(
                "SELECT id FROM protocol_amendments WHERE trial_id=? AND status='pending'", (trial_id,)
            ).fetchone()
            reserved = conn.execute(
                """SELECT revision_no, COUNT(*) AS reserved_unused
                   FROM allocations WHERE trial_id=? AND used_by IS NULL GROUP BY revision_no ORDER BY revision_no""",
                (trial_id,),
            ).fetchall()
            amendment_rows = conn.execute(
                "SELECT * FROM protocol_amendments WHERE trial_id=? ORDER BY revision_no", (trial_id,)
            ).fetchall()
            include_arms = actor["role"] != "site"
            return {
                "trial": {
                    "id": trial["id"], "name": trial["name"],
                    "protocol_version": trial["protocol_version"], "status": trial["status"],
                    "revision_no": trial["revision_no"],
                    "strata_factors": json.loads(trial["strata_factors_json"]),
                    **({"arms": json.loads(trial["arms_json"])} if include_arms else {}),
                },
                "enrollment_paused": pending is not None and trial["status"] == "running",
                "pending_amendment_id": pending["id"] if pending else None,
                "amendments_locked": any(a["status"] == "effective" for a in amendment_rows),
                "amendments": [self._amendment_dict(r, include_arms=include_arms) for r in amendment_rows],
                "reserved_unused_by_revision": [dict(r) for r in reserved],
                "participants_visible": total, "by_site": [dict(x) for x in by_site],
                "audit": [dict(x) | {"detail": json.loads(x["detail"])} for x in audit],
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "Randomization/1.0"
    def _store(self): return self.server.store  # type: ignore[attr-defined]
    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict): raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data
    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _dispatch(self, method):
        path = urlparse(self.path).path.rstrip("/") or "/"; parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", ""); store = self._store()
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes(); self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        if parts == ["api", "trials"] and method == "POST":
            d=self._body(); return self._send(201, store.create_trial(user,d.get("name",""),d.get("protocol_version",""),d.get("arms"),d.get("strata_factors"),d.get("block_size"),d.get("seed","")))
        if len(parts) >= 3 and parts[:2] == ["api", "trials"]:
            trial_id=int(parts[2])
            if len(parts)==4 and parts[3]=="protocol" and method=="POST":
                d=self._body(); return self._send(200, store.update_protocol(user,trial_id,d.get("protocol_version",""),d.get("arms"),d.get("strata_factors"),d.get("block_size"),d.get("seed")))
            if len(parts)==4 and parts[3]=="start" and method=="POST": return self._send(200, store.start_trial(user,trial_id))
            if len(parts)==4 and parts[3]=="participants" and method=="GET": return self._send(200, {"items": store.list_participants(user,trial_id)})
            if len(parts)==4 and parts[3]=="enroll" and method=="POST":
                d=self._body(); return self._send(201, store.enroll(user,trial_id,d.get("external_id",""),d.get("factors",{})))
            if len(parts)==4 and parts[3]=="summary" and method=="GET": return self._send(200, store.trial_summary(user,trial_id))
            if len(parts)==4 and parts[3]=="amendments" and method=="GET": return self._send(200, {"items": store.list_amendments(user,trial_id)})
            if len(parts)==4 and parts[3]=="amendments" and method=="POST":
                d=self._body(); return self._send(201, store.submit_amendment(user,trial_id,d.get("protocol_version",""),d.get("arms"),d.get("strata_factors")))
        if len(parts)==4 and parts[:2]==["api","protocol-amendments"] and parts[3]=="confirm" and method=="POST":
            return self._send(200, store.confirm_amendment(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","protocol-amendments"] and parts[3]=="reject" and method=="POST":
            d=self._body(); return self._send(200, store.reject_amendment(user,int(parts[2]),d.get("reason","")))
        if len(parts)==3 and parts[:2]==["api","participants"] and method=="GET": return self._send(200, store.get_participant(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","participants"] and parts[3]=="unblinding-requests" and method=="POST":
            d=self._body(); return self._send(201, store.request_unblinding(user,int(parts[2]),d.get("reason","")))
        if len(parts)==4 and parts[:2]==["api","unblinding-requests"] and parts[3]=="approve" and method=="POST":
            return self._send(200, store.approve_unblinding(user,int(parts[2])))
        raise BusinessError("接口不存在",404,"not_found")
    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status,{"error":{"code":exc.code,"message":exc.message}})
        except (ValueError,TypeError): self._send(400,{"error":{"code":"invalid_path","message":"路径参数格式错误"}})
        except Exception as exc: self._send(500,{"error":{"code":"internal_error","message":str(exc)}})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class RandomizationServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store=store; super().__init__(address,Handler)


def main():
    parser=argparse.ArgumentParser(description="临床试验随机分配与盲法服务")
    parser.add_argument("--db",default=str(DEFAULT_DB)); parser.add_argument("--port",type=int,default=8104)
    parser.add_argument("--init",action="store_true"); parser.add_argument("--seed",action="store_true")
    args=parser.parse_args(); store=RandomizationStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server=RandomizationServer(("127.0.0.1",args.port),store); print(f"随机化服务运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()
