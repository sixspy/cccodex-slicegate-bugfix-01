
import hashlib
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

# ---------------------------------------------------------------------------
# 裁决边界册口径：只认甲/乙两路，当天选定后写入边界册且当天不得更换，两路不得混壬裁。
#   甲路：裁决接口只更新完整度字段，任何放行态与水位不得在裁决路径改动。
#   乙路：裁决只写完整度预看快照，正式完整度仅在确认提交时写入，仍不得改放行态。
# 禁止裁决成功顺带标已放行；禁止用放行接口冒充裁决。
# 偏离裁决边界册便拦下，并在裁决台账记下“裁决口径偏了”。
# ---------------------------------------------------------------------------
MODE_JIA = "甲"
MODE_YI = "乙"
ADJ_PENDING = "候审"
ADJ_DONE = "已裁决"

# 字段边界：裁决路径只允许落笔完整度一族字段；放行路径只允许落笔放行态。
ADJ_WRITE_FIELDS = {"completeness", "adj_status", "adj_version"}
REL_WRITE_FIELDS = {"release_status"}
# 裁决请求里夹带这些字段 = 想顺带改放行态/水位，属越权，拦下并记“裁决口径偏了”。
FORBIDDEN_IN_ADJUDICATION = {"release_status", "watermark", "watermarks", "release", "releases"}
# 放行请求里夹带这些字段 = 用放行接口冒充裁决，属越权，拦下并记“裁决口径偏了”。
FORBIDDEN_IN_RELEASE = {"completeness", "adj_status", "adj_version", "row_count", "expected_count", "watermark"}


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().date().isoformat()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(slices)").fetchall()}
    if "adj_status" not in cols:
        conn.execute("ALTER TABLE slices ADD COLUMN adj_status TEXT NOT NULL DEFAULT '候审'")
    if "adj_version" not in cols:
        conn.execute("ALTER TABLE slices ADD COLUMN adj_version INTEGER NOT NULL DEFAULT 0")
    # 已放行的历史切片视为已完成裁决，不再进候审队列。
    conn.execute("UPDATE slices SET adj_status='已裁决' WHERE release_status='已放行' AND adj_status='候审'")


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL,
          adj_status TEXT NOT NULL DEFAULT '候审',
          adj_version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        -- 裁决台账：成功/争用/口径偏离全量留痕，与放行流水分账核对。
        CREATE TABLE IF NOT EXISTS adjudications(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER,
          slice_no TEXT NOT NULL,
          adjudicator TEXT NOT NULL,
          mode TEXT NOT NULL,
          action TEXT NOT NULL,
          result TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        -- 裁决边界册：一天一行，当天选定甲/乙后不得更换。
        CREATE TABLE IF NOT EXISTS boundary_register(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          reg_date TEXT UNIQUE NOT NULL,
          mode TEXT NOT NULL,
          set_by TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        -- 乙路完整度预看快照：确认提交前正式完整度不落库。
        CREATE TABLE IF NOT EXISTS previews(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          confirmed INTEGER NOT NULL DEFAULT 0
        );
        """)
        migrate(conn)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行", ADJ_PENDING),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行", ADJ_DONE),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行", ADJ_PENDING),
            ]
            for r in rows:
                conn.execute(
                    "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status,adj_status) VALUES (?,?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")
            conn.execute("INSERT INTO boundary_register(reg_date,mode,set_by,created_at) VALUES ('2026-11-01','甲','admin','2026-11-01T17:00:00')")
            conn.execute("""INSERT INTO adjudications(slice_id,slice_no,adjudicator,mode,action,result,status,note,created_at)
                            VALUES (2,'SL-2026-11-01-west','admin','甲','裁决','完整','成功','甲路裁决落笔：完整度=完整，放行态与水位未动','2026-11-01T17:30:00')""")


@app.on_event("startup")
def _start():
    init_db()


# ---------------------------------------------------------------------------
# 裁决台账与边界册工具
# ---------------------------------------------------------------------------

def log_adjudication(conn, user, action, status, note, slice_id=None, slice_no="-", mode="-", result="-"):
    conn.execute(
        """INSERT INTO adjudications(slice_id,slice_no,adjudicator,mode,action,result,status,note,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (slice_id, slice_no, user, mode, action, result, status, note, now_iso()))


def boundary_today(conn):
    return conn.execute("SELECT * FROM boundary_register WHERE reg_date=?", (today(),)).fetchone()


def reject_deviation(conn, user, note, slice_id=None, slice_no="-", mode="-", action="越权尝试"):
    """偏离裁决边界册：拦下并记下“裁决口径偏了”。"""
    log_adjudication(conn, user, action, "口径偏离", note, slice_id, slice_no, mode)
    conn.commit()
    raise HTTPException(409, note)


def reject_contention(conn, user, slice_id, slice_no, mode, note):
    """同一候审切片被两名值班同时落笔：后手记“裁决争用”，禁静默写成成功。"""
    log_adjudication(conn, user, "裁决", "争用", note, slice_id, slice_no, mode)
    conn.commit()
    raise HTTPException(409, note)


def guard_adjudication_fields(extra, conn, user, slice_id, slice_no):
    bad = sorted(set(extra) & FORBIDDEN_IN_ADJUDICATION)
    if bad:
        reject_deviation(
            conn, user,
            f"裁决口径偏了：裁决路径不得改动放行态与水位（越界字段 {','.join(bad)}），已拦下未写入",
            slice_id, slice_no)


def require_boundary_mode(conn, want, user, slice_id=None, slice_no="-"):
    b = boundary_today(conn)
    if not b:
        reject_deviation(
            conn, user,
            "裁决口径偏了：裁决边界册当天尚未选定口径，须先在策略条选定甲或乙",
            slice_id, slice_no, action="裁决")
    if b["mode"] != want:
        reject_deviation(
            conn, user,
            f"裁决口径偏了：裁决边界册今日定为{b['mode']}路，{want}路不得落笔，两路不得混壬裁",
            slice_id, slice_no, mode=b["mode"], action="裁决")
    return b


def compute_completeness(row_count: int, expected_count: int) -> str:
    if expected_count > 0 and row_count >= expected_count:
        return "完整"
    return "部分完整" if row_count > 0 else "待检"


def get_slice_or_404(conn, sid):
    row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "切片不存在")
    return row


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = compute_completeness(body.row_count, body.expected_count)
    with connect() as c:
        try:
            cur = c.execute(
                """INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status,adj_status)
                   VALUES (?,?,?,?,?,?,'未放行',?)""",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, ADJ_PENDING),
            )
            return {"id": cur.lastrowid, "completeness": comp, "adj_status": ADJ_PENDING}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")


@app.get("/api/slices/{sid}")
def slice_detail(sid: int, authorization: str | None = Header(default=None)):
    """切片侧栏：单切片全字段 + 裁决历史 + 预看快照 + 放行记录，供核对。"""
    require_user(authorization)
    with connect() as c:
        row = get_slice_or_404(c, sid)
        adj = c.execute("SELECT * FROM adjudications WHERE slice_id=? ORDER BY id DESC", (sid,)).fetchall()
        pv = c.execute("SELECT * FROM previews WHERE slice_id=? ORDER BY id DESC", (sid,)).fetchall()
        rel = c.execute("SELECT * FROM releases WHERE slice_id=? ORDER BY id DESC", (sid,)).fetchall()
    return {
        "item": dict(row),
        "adjudications": [dict(r) for r in adj],
        "previews": [dict(r) for r in pv],
        "releases": [dict(r) for r in rel],
    }


# ---------------------------------------------------------------------------
# 裁决边界册（策略条）
# ---------------------------------------------------------------------------

class BoundaryIn(BaseModel):
    mode: str


@app.get("/api/boundary")
def get_boundary(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        cur = boundary_today(c)
        items = c.execute("SELECT * FROM boundary_register ORDER BY id DESC").fetchall()
    return {"today": dict(cur) if cur else None, "items": [dict(r) for r in items]}


@app.post("/api/boundary")
def set_boundary(body: BoundaryIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    mode = body.mode.strip()
    if mode not in (MODE_JIA, MODE_YI):
        raise HTTPException(400, "裁决口径只认甲/乙两路")
    with connect() as c:
        cur = boundary_today(c)
        if cur:
            if cur["mode"] == mode:
                return {"ok": True, "mode": mode, "unchanged": True}
            reject_deviation(
                c, user,
                f"裁决口径偏了：裁决边界册今日已定为{cur['mode']}路，当天不得更换为{mode}路",
                mode=cur["mode"])
        c.execute(
            "INSERT INTO boundary_register(reg_date,mode,set_by,created_at) VALUES (?,?,?,?)",
            (today(), mode, user, now_iso()))
    return {"ok": True, "mode": mode}


# ---------------------------------------------------------------------------
# 完整度裁决（甲路直写 / 乙路预看+确认，与放行分入口）
# ---------------------------------------------------------------------------

class EvaluateIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    base_version: int | None = None


@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, body: EvaluateIn | None = None, authorization: str | None = Header(default=None)):
    """甲路裁决：只更新完整度字段，任何放行态与水位不得在裁决路径改动。"""
    user = require_user(authorization)
    extra = (body.model_extra or {}) if body else {}
    with connect() as c:
        row = get_slice_or_404(c, sid)
        guard_adjudication_fields(extra, c, user, sid, row["slice_no"])
        require_boundary_mode(c, MODE_JIA, user, sid, row["slice_no"])
        if body and body.base_version is not None and body.base_version != row["adj_version"]:
            reject_contention(
                c, user, sid, row["slice_no"], MODE_JIA,
                f"裁决争用：落笔基准版本 v{body.base_version} 已过期（当前 v{row['adj_version']}），本次未写入")
        comp = compute_completeness(row["row_count"], row["expected_count"])
        # 候审切片只许一手落笔：原子状态翻转，后手 rowcount=0 记争用。
        cur = c.execute(
            "UPDATE slices SET completeness=?, adj_status=?, adj_version=adj_version+1 WHERE id=? AND adj_status=?",
            (comp, ADJ_DONE, sid, ADJ_PENDING))
        if cur.rowcount == 0:
            reject_contention(
                c, user, sid, row["slice_no"], MODE_JIA,
                "裁决争用：该候审切片已被其他值班落笔，本次未写入")
        log_adjudication(
            c, user, "裁决", "成功",
            f"甲路裁决落笔：完整度={comp}，放行态与水位未动",
            sid, row["slice_no"], MODE_JIA, comp)
    return {"id": sid, "completeness": comp, "adj_status": ADJ_DONE}


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="allow")


@app.post("/api/slices/{sid}/preview")
def preview(sid: int, body: PreviewIn | None = None, authorization: str | None = Header(default=None)):
    """乙路裁决：只写完整度预看快照，正式完整度与放行态一律不动。"""
    user = require_user(authorization)
    extra = (body.model_extra or {}) if body else {}
    with connect() as c:
        row = get_slice_or_404(c, sid)
        guard_adjudication_fields(extra, c, user, sid, row["slice_no"])
        require_boundary_mode(c, MODE_YI, user, sid, row["slice_no"])
        comp = compute_completeness(row["row_count"], row["expected_count"])
        cur = c.execute(
            "INSERT INTO previews(slice_id,completeness,created_by,created_at) VALUES (?,?,?,?)",
            (sid, comp, user, now_iso()))
        log_adjudication(
            c, user, "预看", "成功",
            f"乙路预看快照落笔：完整度预看={comp}，正式完整度待确认提交，放行态未动",
            sid, row["slice_no"], MODE_YI, comp)
        pid = cur.lastrowid
    return {"preview_id": pid, "completeness": comp}


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    preview_id: int
    base_version: int | None = None


@app.post("/api/slices/{sid}/confirm")
def confirm(sid: int, body: ConfirmIn, authorization: str | None = Header(default=None)):
    """乙路确认提交：此刻才把正式完整度写入切片，仍不得改放行态。"""
    user = require_user(authorization)
    extra = body.model_extra or {}
    with connect() as c:
        row = get_slice_or_404(c, sid)
        guard_adjudication_fields(extra, c, user, sid, row["slice_no"])
        require_boundary_mode(c, MODE_YI, user, sid, row["slice_no"])
        pv = c.execute("SELECT * FROM previews WHERE id=? AND slice_id=?", (body.preview_id, sid)).fetchone()
        if not pv:
            raise HTTPException(404, "预看快照不存在")
        if pv["confirmed"]:
            reject_contention(
                c, user, sid, row["slice_no"], MODE_YI,
                "裁决争用：该预看快照已被确认落笔，本次未写入")
        if body.base_version is not None and body.base_version != row["adj_version"]:
            reject_contention(
                c, user, sid, row["slice_no"], MODE_YI,
                f"裁决争用：落笔基准版本 v{body.base_version} 已过期（当前 v{row['adj_version']}），本次未写入")
        cur = c.execute(
            "UPDATE slices SET completeness=?, adj_status=?, adj_version=adj_version+1 WHERE id=? AND adj_status=?",
            (pv["completeness"], ADJ_DONE, sid, ADJ_PENDING))
        if cur.rowcount == 0:
            reject_contention(
                c, user, sid, row["slice_no"], MODE_YI,
                "裁决争用：该候审切片已被其他值班落笔，本次未写入")
        c.execute("UPDATE previews SET confirmed=1 WHERE id=?", (body.preview_id,))
        log_adjudication(
            c, user, "确认", "成功",
            f"乙路确认提交：正式完整度={pv['completeness']}，放行态与水位未动",
            sid, row["slice_no"], MODE_YI, pv["completeness"])
    return {"id": sid, "completeness": pv["completeness"], "adj_status": ADJ_DONE}


@app.get("/api/adjudications")
def list_adjudications(authorization: str | None = Header(default=None)):
    """裁决列表（裁决台账）：成功/争用/口径偏离全量留痕。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM adjudications ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/previews")
def list_previews(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute(
            "SELECT p.*, s.slice_no FROM previews p JOIN slices s ON s.id=p.slice_id ORDER BY p.id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 下游放行（与裁决分入口；放行接口不得冒充裁决写裁决字段）
# ---------------------------------------------------------------------------

class ReleaseIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    consumer: str


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    extra = body.model_extra or {}
    with connect() as c:
        row = get_slice_or_404(c, sid)
        bad = sorted(set(extra) & FORBIDDEN_IN_RELEASE)
        if bad:
            reject_deviation(
                c, user,
                f"裁决口径偏了：放行接口不得冒充裁决落笔（越界字段 {','.join(bad)}），已拦下未写入",
                sid, row["slice_no"])
        if row["completeness"] != "完整":
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                      (sid, body.consumer, "拒绝", "完整度未达标", now_iso()))
            c.commit()
            raise HTTPException(409, "未完整禁止放行")
        # 放行路径只落放行态 + 放行流水 + 水位，完整度一族字段一律不碰。
        c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                  (sid, body.consumer, "成功", "完整切片放行", now_iso()))
        c.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                  (body.consumer, row["biz_date"], now_iso(), row["slice_no"]))
    return {"ok": True}


@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    """放行台账（放行流水）。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
