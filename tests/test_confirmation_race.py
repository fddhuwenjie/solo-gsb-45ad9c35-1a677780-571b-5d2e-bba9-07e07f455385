"""并发回归：人工确认歧义记录的竞态与每个 location 唯一确认约束。

修复前，``POST .../confirmations`` 在写事务**之外**完成「是否已确认」与候选
校验，且 ``confirmations.location_id`` 无唯一约束：两个并发请求可能各自选定
（甚至不同的）候选并都返回 201，留下两条确认；后续查询只取其中一条，首条
结果被悄悄遮蔽。

修复后：

- 目标版本 / 迁移记录读取、状态 / 顺延 / 候选校验、既有确认检查与插入全部在
  **同一个 BEGIN IMMEDIATE 写事务**内完成，并发确认由 SQLite 写锁串行化；
- ``confirmations.location_id`` 带唯一索引（``idx_confirm_loc_unique``），
  数据库层兜底保证每个 location 至多一条确认；
- 竞争失败者在锁内重新读到首条确认，**稳定返回 409**，不覆盖首条、不留重复行。

本文件用 monkeypatch 钩子 + threading.Event 做**确定性可控交错**，覆盖：

1. 两请求选择**不同候选**：先入事务者 201，被写锁挡住者 409，只有一条确认，
   生效范围即首条所选候选；
2. 两请求选择**相同候选**：同样恰有一条 201、一条 409；
3. 无钩子多线程突发（候选交替）：始终恰有一条 201；
4. 直接在 SQLite 层插入第二条确认被唯一索引拒绝；
5. 旧库迁移：无重复库平滑建立唯一索引（幂等、数据不丢）；历史有重复时
   拒绝启动、不删改任何行，并报告可定位的 location / 确认行 / 归属信息。
"""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


V1 = "aa目标bb"
SEL = "目标"
# 两处上下文完全相同 → ambiguous，候选为 [2,4) 与 [8,10)
V2 = "xx目标yyxx目标yy"
CAND_A = {"start": 2, "end": 4}
CAND_B = {"start": 8, "end": 10}


def _make_ambiguous_doc(client):
    """建文档 + 批注 + 提交产生歧义的第 2 版，返回 (doc, ann, location_id)。"""
    r = client.post(
        "/documents", json={"title": "确认并发文档", "content": V1}
    )
    assert r.status_code == 201, r.text
    doc = r.json()["document_id"]

    start = V1.index(SEL)
    r = client.post(
        f"/documents/{doc}/annotations",
        json={
            "body": "并发确认",
            "selection": SEL,
            "left_context": "aa",
            "right_context": "bb",
            "start": start,
            "end": start + len(SEL),
        },
    )
    assert r.status_code == 201, r.text
    ann = r.json()["annotation_id"]

    r = client.post(f"/documents/{doc}/versions", json={"content": V2})
    assert r.status_code == 201, r.text
    res = r.json()["results"][0]
    assert res["status"] == "ambiguous"
    assert len(res["candidates"]) == 2

    hist = client.get(
        f"/documents/{doc}/annotations/{ann}/history"
    ).json()["history"]
    v2_entry = next(h for h in hist if h["version_number"] == 2)
    assert v2_entry["status"] == "ambiguous"
    return doc, ann, v2_entry["location_id"]


def _post_confirmation(doc, ann, candidate, note):
    # 独立线程使用独立 TestClient，对应线上两个请求各自持有独立连接
    return TestClient(app).post(
        f"/documents/{doc}/annotations/{ann}/confirmations",
        json={
            "version_number": 2,
            "start": candidate["start"],
            "end": candidate["end"],
            "note": note,
        },
    )


class _OneShotGate:
    """一次性交错闸门：首次调用时阻塞，直到测试主线程放行；后续调用直接通过。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = True

    def wait_inside(self):
        if self._armed:
            self._armed = False
            self.entered.set()
            assert self.release.wait(timeout=10), "交错闸门等待超时"


def _run_controlled_interleaving(client, monkeypatch, candidate_a, candidate_b):
    """确定性交错：A 在写事务内（持写锁、插入前）被闸门挡住，B 在 BEGIN
    IMMEDIATE 处等锁；放行后 A 先提交，B 在锁内重查并失败。

    返回 (response_a, response_b, doc, ann, location_id)。
    """
    doc, ann, location_id = _make_ambiguous_doc(client)
    gate = _OneShotGate()
    original = main._confirmation_of

    def gated_confirmation_of(con, loc_id):
        # confirm 接口的既有确认检查发生在写事务内：
        # A 命中闸门时已持有写锁但尚未插入
        gate.wait_inside()
        return original(con, loc_id)

    monkeypatch.setattr(main, "_confirmation_of", gated_confirmation_of)
    outcome_a = {}
    t_a = threading.Thread(
        target=lambda: outcome_a.setdefault(
            "r", _post_confirmation(doc, ann, candidate_a, "A 的选择")
        )
    )
    t_b = None
    t_a.start()
    try:
        assert gate.entered.wait(timeout=10), "A 未进入写事务内检查点"

        # B 在 A 持写锁期间发起：BEGIN IMMEDIATE 必须等锁，无法抢先插入
        outcome_b = {}
        t_b = threading.Thread(
            target=lambda: outcome_b.setdefault(
                "r", _post_confirmation(doc, ann, candidate_b, "B 的选择")
            )
        )
        t_b.start()
        t_b.join(timeout=1)
        assert t_b.is_alive(), "B 不应在 A 持有写锁期间完成确认"
    finally:
        gate.release.set()
        t_a.join(timeout=10)
        if t_b is not None:
            t_b.join(timeout=10)

    assert not t_a.is_alive()
    assert t_b is not None and not t_b.is_alive()
    return outcome_a["r"], outcome_b["r"], doc, ann, location_id


def _assert_only_first_wins(
    client, r_a, r_b, doc, ann, location_id, winner_candidate
):
    assert r_a.status_code == 201, r_a.text
    assert r_b.status_code == 409, r_b.text
    assert "已确认" in r_b.json()["detail"]

    # 首条不被覆盖：生效范围即 A 所选候选，匹配依据（全部候选）仍保留
    loc = r_a.json()["location"]
    assert loc["status"] == "confirmed"
    assert loc["start"] == winner_candidate["start"]
    assert loc["end"] == winner_candidate["end"]
    assert loc["location_id"] == location_id
    assert len(loc["candidates"]) == 2

    # 库中每个 location 恰有一条确认
    con = db.connect()
    try:
        count = con.execute(
            "SELECT COUNT(*) AS c FROM confirmations WHERE location_id = ?",
            (location_id,),
        ).fetchone()["c"]
    finally:
        con.close()
    assert count == 1

    # 后续查询稳定呈现首条
    hist = client.get(
        f"/documents/{doc}/annotations/{ann}/history"
    ).json()["history"]
    v2_entry = next(h for h in hist if h["version_number"] == 2)
    assert v2_entry["status"] == "confirmed"
    assert v2_entry["start"] == winner_candidate["start"]
    assert v2_entry["end"] == winner_candidate["end"]
    assert v2_entry["confirmation"]["note"] == "A 的选择"


def test_concurrent_different_candidates_only_first_wins(client, monkeypatch):
    """不同候选并发：A 选前一处 201，B 选后一处 409，不留下第二条确认。"""
    r_a, r_b, doc, ann, location_id = _run_controlled_interleaving(
        client, monkeypatch, CAND_A, CAND_B
    )
    _assert_only_first_wins(client, r_a, r_b, doc, ann, location_id, CAND_A)


def test_concurrent_same_candidate_only_first_wins(client, monkeypatch):
    """相同候选并发：同样恰有一条 201 与一条稳定 409。"""
    r_a, r_b, doc, ann, location_id = _run_controlled_interleaving(
        client, monkeypatch, CAND_A, CAND_A
    )
    _assert_only_first_wins(client, r_a, r_b, doc, ann, location_id, CAND_A)


def test_concurrent_burst_always_exactly_one_confirmation(client):
    """无钩子突发：多线程（候选交替）下始终恰有一条 201，其余稳定 409。"""
    doc, ann, location_id = _make_ambiguous_doc(client)
    outcomes = {}
    threads = []
    for i in range(6):
        candidate = CAND_A if i % 2 == 0 else CAND_B

        def worker(index=i, cand=candidate):
            outcomes[index] = _post_confirmation(doc, ann, cand, f"候选 {index}")

        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    statuses = [r.status_code for r in outcomes.values()]
    assert statuses.count(201) == 1, {
        k: (v.status_code, v.text) for k, v in outcomes.items()
    }
    assert statuses.count(409) == 5
    con = db.connect()
    try:
        count = con.execute(
            "SELECT COUNT(*) AS c FROM confirmations WHERE location_id = ?",
            (location_id,),
        ).fetchone()["c"]
    finally:
        con.close()
    assert count == 1


def test_database_layer_rejects_second_confirmation(client):
    """绕过 API 直接写入第二条确认：SQLite 唯一索引拒绝（IntegrityError）。"""
    doc, ann, location_id = _make_ambiguous_doc(client)
    r = _post_confirmation(doc, ann, CAND_A, "首条")
    assert r.status_code == 201, r.text

    con = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                'INSERT INTO confirmations (location_id, start, "end", note)'
                " VALUES (?, ?, ?, ?)",
                (location_id, CAND_B["start"], CAND_B["end"], "绕过 API 的重复"),
            )
    finally:
        con.close()

    con = db.connect()
    try:
        count = con.execute(
            "SELECT COUNT(*) AS c FROM confirmations WHERE location_id = ?",
            (location_id,),
        ).fetchone()["c"]
    finally:
        con.close()
    assert count == 1


# ---------------------------------------------------------------------------
# 旧库迁移：唯一索引的平滑建立与历史重复的不删改报告
# ---------------------------------------------------------------------------


def _legacy_schema_script():
    """修复前版本的建库结果：全部表 + confirmations 上的旧非唯一索引。

    当前 SCHEMA 已不再创建任何 confirmations 索引，因此先执行它得到表，
    再补建旧版非唯一索引，即等价于线上升级前的真实库结构。
    """
    return db.SCHEMA + (
        "CREATE INDEX IF NOT EXISTS idx_confirm_loc ON confirmations (location_id);"
    )


def _seed_legacy_db(con, *, with_duplicate):
    con.executescript(_legacy_schema_script())
    con.execute("INSERT INTO documents (id, title) VALUES (1, '旧库文档')")
    con.execute(
        "INSERT INTO versions (id, document_id, number, content) VALUES"
        " (1, 1, 1, 'aa目标bb'),"
        " (2, 1, 2, 'xx目标yyxx目标yy')"
    )
    con.execute(
        """
        INSERT INTO annotations
            (id, document_id, body, selection, left_context, right_context,
             orig_start, orig_end, created_version_id)
        VALUES (4, 1, '旧批注', '目标', 'aa', 'bb', 2, 4, 1)
        """
    )
    con.execute(
        """
        INSERT INTO locations
            (id, annotation_id, version_id, status, score, candidates, carried_from)
        VALUES (7, 4, 2, 'ambiguous', 4,
                '[{"start":2,"end":4,"left_match":2,"right_match":2,"score":4},'
                || ' {"start":8,"end":10,"left_match":2,"right_match":2,"score":4}]',
                NULL)
        """
    )
    con.execute(
        'INSERT INTO confirmations (id, location_id, start, "end", note) '
        "VALUES (10, 7, 2, 4, '首条确认')"
    )
    if with_duplicate:
        # 修复前无唯一约束时并发可能留下的第二条（选了另一候选）
        con.execute(
            'INSERT INTO confirmations (id, location_id, start, "end", note) '
            "VALUES (11, 7, 8, 10, '重复确认')"
        )


def _index_names(con):
    return [
        (row["name"], bool(row["unique"]))
        for row in con.execute("PRAGMA index_list(confirmations)").fetchall()
    ]


def test_migration_creates_unique_index_on_clean_legacy_db(tmp_path, monkeypatch):
    """无重复旧库：平滑建立唯一索引、摘除旧非唯一索引；重复启动幂等、数据保留。"""
    path = tmp_path / "legacy-clean.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    con = db.connect()
    try:
        _seed_legacy_db(con, with_duplicate=False)
        assert ("idx_confirm_loc", False) in _index_names(con)
    finally:
        con.close()

    db.init_db()
    con = db.connect()
    try:
        names = _index_names(con)
        assert (db.CONFIRMATION_UNIQUE_INDEX, True) in names
        assert not any(name == db.CONFIRMATION_LEGACY_INDEX for name, _ in names)
        # 数据无损
        assert (
            con.execute("SELECT COUNT(*) AS c FROM confirmations").fetchone()["c"]
            == 1
        )
        row = con.execute(
            "SELECT location_id, start, note FROM confirmations WHERE id = 10"
        ).fetchone()
        assert (row["location_id"], row["start"], row["note"]) == (7, 2, "首条确认")
    finally:
        con.close()

    # 再次启动（模拟重启）：已存在唯一索引，直接返回、无副作用
    db.init_db()
    con = db.connect()
    try:
        assert (db.CONFIRMATION_UNIQUE_INDEX, True) in _index_names(con)
    finally:
        con.close()


def test_migration_reports_duplicate_locations_without_touching_rows(
    tmp_path, monkeypatch
):
    """历史重复库：拒绝建立约束并抛出可定位报告，绝不静默删改任何行。"""
    path = tmp_path / "legacy-dup.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    con = db.connect()
    try:
        _seed_legacy_db(con, with_duplicate=True)
    finally:
        con.close()

    with pytest.raises(db.DuplicateConfirmationsError) as exc_info:
        db.init_db()

    dup = exc_info.value.duplicates
    assert len(dup) == 1
    item = dup[0]
    # 可定位：location 及其文档 / 批注 / 版本归属，重复行 ID 与各自选择
    assert item["location_id"] == 7
    assert item["document_id"] == 1
    assert item["annotation_id"] == 4
    assert item["version_number"] == 2
    assert item["confirmation_ids"] == [10, 11]
    assert [c["id"] for c in item["confirmations"]] == [10, 11]
    assert [c["start"] for c in item["confirmations"]] == [2, 8]
    assert "7" in str(exc_info.value)

    # 重复行原封不动，唯一索引未建立（否则重复数据下不可能建成功）
    con = db.connect()
    try:
        assert (
            con.execute("SELECT COUNT(*) AS c FROM confirmations").fetchone()["c"]
            == 2
        )
        names = _index_names(con)
        assert not any(
            name == db.CONFIRMATION_UNIQUE_INDEX and unique for name, unique in names
        )
        kept = con.execute(
            "SELECT id, start, note FROM confirmations ORDER BY id"
        ).fetchall()
        assert [(r["id"], r["start"], r["note"]) for r in kept] == [
            (10, 2, "首条确认"),
            (11, 8, "重复确认"),
        ]
    finally:
        con.close()


def test_fresh_database_gets_unique_index(tmp_path, monkeypatch):
    """全新库：init_db 即建立唯一索引，应用层与库层约束同时生效。"""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    con = db.connect()
    try:
        assert (db.CONFIRMATION_UNIQUE_INDEX, True) in _index_names(con)
    finally:
        con.close()
