
import pytest
from fastapi.testclient import TestClient
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c, u="admin", p="123456"):
    r = c.post("/api/login", json={"username": u, "password": p})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk(c, h, no="SL-X-1", rows=10, exp=10):
    r = c.post("/api/slices", headers=h,
               json={"slice_no": no, "biz_date": "2026-11-05", "partition_key": "mid",
                     "row_count": rows, "expected_count": exp})
    assert r.status_code == 200
    return r.json()["id"]


def _slice(c, h, sid):
    return [x for x in c.get("/api/slices", headers=h).json()["items"] if x["id"] == sid][0]


def _ledger(c, h):
    return c.get("/api/adjudications", headers=h).json()["items"]


def test_boundary_once_per_day_and_change_recorded():
    c = TestClient(app)
    h = _login(c)
    assert c.post("/api/boundary", headers=h, json={"mode": "甲"}).status_code == 200
    # 同口径重复选定幂等
    assert c.post("/api/boundary", headers=h, json={"mode": "甲"}).status_code == 200
    # 当天不得更换，且偏离要记“裁决口径偏了”
    r = c.post("/api/boundary", headers=h, json={"mode": "乙"})
    assert r.status_code == 409
    assert "裁决口径偏了" in r.json()["detail"]
    assert any(a["status"] == "口径偏离" and "裁决口径偏了" in a["note"] for a in _ledger(c, h))
    assert c.get("/api/boundary", headers=h).json()["today"]["mode"] == "甲"


def test_jia_evaluate_only_writes_completeness():
    c = TestClient(app)
    h = _login(c)
    c.post("/api/boundary", headers=h, json={"mode": "甲"})
    sid = _mk(c, h, rows=5, exp=10)
    r = c.post(f"/api/slices/{sid}/evaluate", headers=h, json={"base_version": 0})
    assert r.status_code == 200
    assert r.json()["completeness"] == "部分完整"
    s = _slice(c, h, sid)
    assert s["completeness"] == "部分完整"
    assert s["adj_status"] == "已裁决"
    # 裁决路径不得顺带改放行态与水位
    assert s["release_status"] == "未放行"
    assert all(w["source_slice"] != "SL-X-1" for w in c.get("/api/watermarks", headers=h).json()["items"])
    assert all(x["slice_id"] != sid for x in c.get("/api/releases", headers=h).json()["items"])


def test_contention_recorded_never_silent_success():
    c = TestClient(app)
    h = _login(c)
    c.post("/api/boundary", headers=h, json={"mode": "甲"})
    sid = _mk(c, h)
    first = c.post(f"/api/slices/{sid}/evaluate", headers=h, json={"base_version": 0})
    assert first.status_code == 200
    # 后手用旧基准版本落笔 -> 裁决争用，禁静默写成成功
    second = c.post(f"/api/slices/{sid}/evaluate", headers=h, json={"base_version": 0})
    assert second.status_code == 409
    assert "裁决争用" in second.json()["detail"]
    adj = [a for a in _ledger(c, h) if a["slice_id"] == sid]
    assert any(a["status"] == "争用" and "裁决争用" in a["note"] for a in adj)
    assert len([a for a in adj if a["status"] == "成功"]) == 1


def test_yi_preview_then_confirm_and_no_mixing():
    c = TestClient(app)
    h = _login(c)
    c.post("/api/boundary", headers=h, json={"mode": "乙"})
    sid = _mk(c, h, rows=5, exp=10)
    # 乙路当天，甲路直写属混壬裁 -> 拦下并记
    r = c.post(f"/api/slices/{sid}/evaluate", headers=h, json={})
    assert r.status_code == 409
    assert "裁决口径偏了" in r.json()["detail"]
    # 预看只写快照，正式完整度不落库
    p = c.post(f"/api/slices/{sid}/preview", headers=h, json={})
    assert p.status_code == 200
    pid = p.json()["preview_id"]
    s = _slice(c, h, sid)
    assert s["adj_status"] == "候审"
    # 确认提交才落正式完整度，放行态仍不动
    cf = c.post(f"/api/slices/{sid}/confirm", headers=h, json={"preview_id": pid, "base_version": 0})
    assert cf.status_code == 200
    s = _slice(c, h, sid)
    assert s["adj_status"] == "已裁决"
    assert s["release_status"] == "未放行"
    # 重复确认 -> 争用
    again = c.post(f"/api/slices/{sid}/confirm", headers=h, json={"preview_id": pid})
    assert again.status_code == 409
    assert "裁决争用" in again.json()["detail"]


def test_unset_boundary_blocks_and_records():
    c = TestClient(app)
    h = _login(c)
    sid = _mk(c, h)
    r = c.post(f"/api/slices/{sid}/evaluate", headers=h, json={})
    assert r.status_code == 409
    assert "裁决口径偏了" in r.json()["detail"]
    assert any("裁决口径偏了" in a["note"] for a in _ledger(c, h))
    assert _slice(c, h, sid)["adj_status"] == "候审"


def test_evaluate_cannot_smuggle_release_fields():
    c = TestClient(app)
    h = _login(c)
    c.post("/api/boundary", headers=h, json={"mode": "甲"})
    sid = _mk(c, h)
    r = c.post(f"/api/slices/{sid}/evaluate", headers=h, json={"release_status": "已放行"})
    assert r.status_code == 409
    assert "裁决口径偏了" in r.json()["detail"]
    s = _slice(c, h, sid)
    assert s["release_status"] == "未放行"
    assert s["adj_status"] == "候审"


def test_release_cannot_impersonate_adjudication():
    c = TestClient(app)
    h = _login(c)
    c.post("/api/boundary", headers=h, json={"mode": "甲"})
    sid = _mk(c, h, rows=5, exp=10)
    r = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "x", "completeness": "完整"})
    assert r.status_code == 409
    assert "裁决口径偏了" in r.json()["detail"]
    # 放行接口没有冒充裁决改完整度
    assert _slice(c, h, sid)["completeness"] == "部分完整"
    assert any("裁决口径偏了" in a["note"] for a in _ledger(c, h))


def test_release_does_not_touch_completeness():
    c = TestClient(app)
    h = _login(c)
    c.post("/api/boundary", headers=h, json={"mode": "甲"})
    sid = _mk(c, h)
    c.post(f"/api/slices/{sid}/evaluate", headers=h, json={})
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"}).status_code == 200
    s = _slice(c, h, sid)
    assert s["release_status"] == "已放行"
    assert s["completeness"] == "完整"
    # 裁决台账与放行流水各司其职：该切片裁决 1 笔成功、放行 1 笔成功
    adj = [a for a in _ledger(c, h) if a["slice_id"] == sid and a["status"] == "成功"]
    rel = [x for x in c.get("/api/releases", headers=h).json()["items"] if x["slice_id"] == sid and x["status"] == "成功"]
    assert len(adj) == 1 and len(rel) == 1
