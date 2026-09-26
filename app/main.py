"""文档修订批注续接 API。

- 保存基准文本（文档第 1 版）、批注正文与锚点
  （选区 + 左右上下文 + 按 Unicode 码点计的原始偏移）；
- 提交新版全文时，为每条批注枚举选区的全部精确落点，
  按「左上下文后缀匹配长度 + 右上下文前缀匹配长度」之和排序：
  唯一最高分 → resolved（自动续接）；
  并列最高分 → ambiguous（返回全部候选范围，需人工确认）；
  找不到选区 → unresolved；
- 歧义批注在人工确认前不参与后续版本的自动迁移（顺延为占位记录）；
- 版本与迁移结果只追加，可查询任意两版间每条批注的范围、状态与匹配依据；
- 改动影响分析：指定起止版本，基于两版全文生成稳定的码点级编辑片段，
  把起始版每条批注的范围投影到目标版并分类
  （unchanged / shifted / modified / deleted / indeterminate）；
- 审阅依赖编排：批量提交同一文档内「前置批注 → 后续批注」关系
  （整批校验跨文档引用、自依赖、重复边与环，任一失败则全部不落库）；
  审阅计划按依赖图与完成记录把批注划分为可处理 / 被阻塞 / 已完成，
  并按拓扑层级给出可并行批次；完成记录只追加、重复提交幂等，
  完成前置批注后立即解锁符合条件的后续批注；
- 迁移归档：同一读事务导出文档元数据、全部版本、批注、迁移记录与人工
  确认的规范 JSON 及 SHA-256 摘要；核验接口只依据归档与摘要离线复算
  摘要并检查版本连续性、记录归属、每版迁移记录、顺延引用与人工确认的
  一致性，缺行、串文档、篡改或摘要不符时返回可定位错误，不写入数据；
- 文档发布门禁：按名称只追加地保存嵌套策略修订（all / any / not 与
  location_count / review_count 叶子，深度与节点数受限）；评估接口在
  同一个 SQLite 读事务内锁定策略修订、目标文档版本与当前审阅状态，
  返回不短路的同形解释树（实际值、阈值与参与计数的批注 ID）；
- 批注驱动的脱敏副本：指定文档版本、批注 ID 集合与占位文本，在同一个
  SQLite 读事务内读取版本全文与各批注的生效位置（只接受 resolved /
  confirmed），把重叠或首尾相接的范围合并为最大区间并按起点顺序替换，
  返回脱敏全文与源 / 结果码点半开区间映射（未遮盖片段同样可追溯）；
  跨文档、缺少位置、ambiguous / unresolved 等情况整批返回可定位错误，
  不猜测、不输出半成品，接口只读、不写入数据，相同快照与请求结果字节一致。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from . import archive as archive_mod
from . import db
from . import gate as gate_mod
from .diffing import (
    classify_range,
    compute_segments,
    hit_segment_indices,
    project_range,
)
from .matching import locate
from .redacting import EffectiveRange, build_redaction, merge_ranges
from .review import (
    build_prereq_map,
    find_cycle_path,
    topological_waves,
    transitive_prerequisites,
)
from .schemas import (
    AnnotationCreate,
    ArchiveVerifyRequest,
    CompletionCreate,
    ConfirmationCreate,
    DependencyBatchCreate,
    DocumentCreate,
    PolicyCreate,
    RedactionCreate,
    VersionCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="文档修订批注续接 API",
    version="1.5.0",
    description=(
        "基于选区 + 左右上下文锚点的批注跨版本续接，"
        "以及批注间的审阅依赖编排（依赖图校验、审阅计划、完成记录）。"
        "所有偏移均按 Unicode 码点计；版本、迁移结果、依赖关系、完成记录"
        "与门禁策略修订只追加。"
        "另提供可离线核验的迁移归档：同一读事务导出规范 JSON 与 SHA-256 摘要，"
        "核验只依据归档与摘要本身，不查数据库、不写入数据；"
        "以及文档发布门禁：策略修订按名称只追加，评估在同一读事务内锁定"
        "策略修订、目标版本与当前审阅状态，返回同形解释树；"
        "以及批注驱动的脱敏副本：同一读事务读取版本与生效位置，"
        "重叠或首尾相接的范围合并替换，返回脱敏全文与码点区间映射。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error_handler(request, exc):
    return JSONResponse(status_code=409, content={"detail": f"数据冲突：{exc}"})


def get_db():
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------


def _get_document(con: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    doc = con.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if doc is None:
        raise HTTPException(404, f"文档 {document_id} 不存在")
    return doc


def _get_annotation(
    con: sqlite3.Connection, document_id: int, annotation_id: int
) -> sqlite3.Row:
    ann = con.execute(
        "SELECT * FROM annotations WHERE id = ? AND document_id = ?",
        (annotation_id, document_id),
    ).fetchone()
    if ann is None:
        raise HTTPException(404, f"文档 {document_id} 中不存在批注 {annotation_id}")
    return ann


def _get_version(con: sqlite3.Connection, document_id: int, number: int) -> sqlite3.Row:
    v = con.execute(
        "SELECT * FROM versions WHERE document_id = ? AND number = ?",
        (document_id, number),
    ).fetchone()
    if v is None:
        raise HTTPException(404, f"文档 {document_id} 不存在第 {number} 版")
    return v


def _latest_version(con: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    return con.execute(
        "SELECT * FROM versions WHERE document_id = ? ORDER BY number DESC LIMIT 1",
        (document_id,),
    ).fetchone()


def _confirmation_of(con: sqlite3.Connection, location_id: int) -> Optional[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM confirmations WHERE location_id = ? ORDER BY id DESC LIMIT 1",
        (location_id,),
    ).fetchone()


def _location_out(con: sqlite3.Connection, loc: sqlite3.Row) -> dict:
    """迁移记录的完整视图：范围、状态与匹配依据（含确认后的生效值）。"""
    version_number = con.execute(
        "SELECT number FROM versions WHERE id = ?", (loc["version_id"],)
    ).fetchone()["number"]
    conf = _confirmation_of(con, loc["id"])
    out = {
        "location_id": loc["id"],
        "annotation_id": loc["annotation_id"],
        "version_number": version_number,
        "status": loc["status"],
        "start": loc["start"],
        "end": loc["end"],
        "left_match": loc["left_match"],
        "right_match": loc["right_match"],
        "score": loc["score"],
        "candidates": json.loads(loc["candidates"]),
        "carried_forward": loc["carried_from"] is not None,
        "carried_from_location_id": loc["carried_from"],
        "confirmation": None,
        "created_at": loc["created_at"],
    }
    if conf is not None:
        # 确认后的生效状态与范围
        out["status"] = "confirmed"
        out["start"] = conf["start"]
        out["end"] = conf["end"]
        out["confirmation"] = {
            "start": conf["start"],
            "end": conf["end"],
            "note": conf["note"],
            "created_at": conf["created_at"],
        }
    return out


def _annotation_out(con: sqlite3.Connection, ann: sqlite3.Row) -> dict:
    latest = con.execute(
        """
        SELECT l.* FROM locations l
        JOIN versions v ON v.id = l.version_id
        WHERE l.annotation_id = ?
        ORDER BY v.number DESC LIMIT 1
        """,
        (ann["id"],),
    ).fetchone()
    created_number = con.execute(
        "SELECT number FROM versions WHERE id = ?", (ann["created_version_id"],)
    ).fetchone()["number"]
    return {
        "annotation_id": ann["id"],
        "document_id": ann["document_id"],
        "body": ann["body"],
        "anchor": {
            "selection": ann["selection"],
            "left_context": ann["left_context"],
            "right_context": ann["right_context"],
            "start": ann["orig_start"],
            "end": ann["orig_end"],
            "version_number": created_number,
        },
        "created_at": ann["created_at"],
        "latest": _location_out(con, latest) if latest else None,
    }


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


@app.get("/")
def root():
    return {
        "service": "文档修订批注续接 API",
        "version": "1.5.0",
        "docs": "/docs",
        "offsets": "所有偏移均按 Unicode 码点计",
        "archive_format": archive_mod.ARCHIVE_FORMAT,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 文档与版本
# ---------------------------------------------------------------------------


@app.post("/documents", status_code=201)
def create_document(payload: DocumentCreate, con=Depends(get_db)):
    """新建文档；content 作为基准文本保存为第 1 版。"""
    with db.transaction(con):
        cur = con.execute("INSERT INTO documents (title) VALUES (?)", (payload.title,))
        document_id = cur.lastrowid
        cur = con.execute(
            "INSERT INTO versions (document_id, number, content) VALUES (?, 1, ?)",
            (document_id, payload.content),
        )
        version_id = cur.lastrowid
    return {
        "document_id": document_id,
        "title": payload.title,
        "version": {"version_id": version_id, "version_number": 1},
    }


@app.get("/documents/{document_id}")
def get_document(document_id: int, con=Depends(get_db)):
    doc = _get_document(con, document_id)
    versions = con.execute(
        """
        SELECT number, length(content) AS length, created_at
        FROM versions WHERE document_id = ? ORDER BY number
        """,
        (document_id,),
    ).fetchall()
    ann_count = con.execute(
        "SELECT COUNT(*) AS c FROM annotations WHERE document_id = ?", (document_id,)
    ).fetchone()["c"]
    return {
        "document_id": doc["id"],
        "title": doc["title"],
        "created_at": doc["created_at"],
        "annotation_count": ann_count,
        "versions": [dict(v) for v in versions],
    }


@app.get("/documents/{document_id}/versions")
def list_versions(document_id: int, con=Depends(get_db)):
    _get_document(con, document_id)
    rows = con.execute(
        """
        SELECT id, number, length(content) AS length, created_at
        FROM versions WHERE document_id = ? ORDER BY number
        """,
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "versions": [
            {
                "version_id": r["id"],
                "version_number": r["number"],
                "length": r["length"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.get("/documents/{document_id}/versions/{number}")
def get_version(document_id: int, number: int, con=Depends(get_db)):
    """某版全文及该版上每条批注的迁移结果。"""
    _get_document(con, document_id)
    v = _get_version(con, document_id, number)
    locs = con.execute(
        "SELECT * FROM locations WHERE version_id = ? ORDER BY annotation_id",
        (v["id"],),
    ).fetchall()
    return {
        "document_id": document_id,
        "version_number": v["number"],
        "content": v["content"],
        "created_at": v["created_at"],
        "locations": [_location_out(con, loc) for loc in locs],
    }


@app.post("/documents/{document_id}/versions", status_code=201)
def create_version(document_id: int, payload: VersionCreate, con=Depends(get_db)):
    """提交新版全文，并为每条批注执行一次续接迁移（结果只追加）。

    规则：
    - 上一条「真实」迁移结果为 ambiguous 且未确认 → 不参与本次自动迁移，
      追加一条顺延占位记录（carried_forward=true）；
    - 其余（resolved / 已确认 / unresolved）均重新匹配：
      选区重新出现时 unresolved 批注也会自动恢复。
    """
    _get_document(con, document_id)
    results = []
    with db.transaction(con):
        number = con.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM versions WHERE document_id = ?",
            (document_id,),
        ).fetchone()["n"]
        cur = con.execute(
            "INSERT INTO versions (document_id, number, content) VALUES (?, ?, ?)",
            (document_id, number, payload.content),
        )
        version_id = cur.lastrowid

        annotations = con.execute(
            "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()

        for ann in annotations:
            # 最近一次真实（非顺延）迁移结果
            prev = con.execute(
                """
                SELECT l.* FROM locations l
                JOIN versions v ON v.id = l.version_id
                WHERE l.annotation_id = ? AND l.carried_from IS NULL
                ORDER BY v.number DESC LIMIT 1
                """,
                (ann["id"],),
            ).fetchone()

            if (
                prev is not None
                and prev["status"] == "ambiguous"
                and _confirmation_of(con, prev["id"]) is None
            ):
                # 歧义未确认：不参与自动迁移，顺延占位
                con.execute(
                    """
                    INSERT INTO locations (annotation_id, version_id, status, carried_from)
                    VALUES (?, ?, 'ambiguous', ?)
                    """,
                    (ann["id"], version_id, prev["id"]),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "ambiguous",
                        "carried_forward": True,
                        "detail": "存在未确认的歧义，本次不参与自动迁移",
                    }
                )
                continue

            status, best, candidates = locate(
                payload.content,
                ann["selection"],
                ann["left_context"],
                ann["right_context"],
            )
            candidates_json = json.dumps(
                [c.to_dict() for c in candidates], ensure_ascii=False
            )
            if status == "resolved":
                con.execute(
                    """
                    INSERT INTO locations
                        (annotation_id, version_id, status, start, "end",
                         left_match, right_match, score, candidates)
                    VALUES (?, ?, 'resolved', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ann["id"],
                        version_id,
                        best.start,
                        best.end,
                        best.left_match,
                        best.right_match,
                        best.score,
                        candidates_json,
                    ),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "resolved",
                        "carried_forward": False,
                        "start": best.start,
                        "end": best.end,
                        "left_match": best.left_match,
                        "right_match": best.right_match,
                        "score": best.score,
                        "candidates": [c.to_dict() for c in candidates],
                    }
                )
            elif status == "ambiguous":
                top_score = candidates[0].score
                con.execute(
                    """
                    INSERT INTO locations
                        (annotation_id, version_id, status, score, candidates)
                    VALUES (?, ?, 'ambiguous', ?, ?)
                    """,
                    (ann["id"], version_id, top_score, candidates_json),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "ambiguous",
                        "carried_forward": False,
                        "score": top_score,
                        "candidates": [c.to_dict() for c in candidates],
                        "detail": "多个候选并列最高分，需人工确认",
                    }
                )
            else:  # unresolved
                con.execute(
                    """
                    INSERT INTO locations (annotation_id, version_id, status, candidates)
                    VALUES (?, ?, 'unresolved', '[]')
                    """,
                    (ann["id"], version_id),
                )
                results.append(
                    {
                        "annotation_id": ann["id"],
                        "status": "unresolved",
                        "carried_forward": False,
                        "detail": "新版本中找不到选区的精确落点",
                    }
                )

    summary = {}
    for r in results:
        key = "carried" if r.get("carried_forward") else r["status"]
        summary[key] = summary.get(key, 0) + 1
    return {
        "document_id": document_id,
        "version": {"version_id": version_id, "version_number": number},
        "summary": summary,
        "results": results,
    }


# ---------------------------------------------------------------------------
# 批注
# ---------------------------------------------------------------------------


@app.post("/documents/{document_id}/annotations", status_code=201)
def create_annotation(document_id: int, payload: AnnotationCreate, con=Depends(get_db)):
    """在当前最新版上创建批注；锚点会与基准文本严格校验。

    最新版读取、锚点校验与批注 / 初始迁移记录的写入在同一个
    BEGIN IMMEDIATE 写事务中完成：以取得写锁后的真实最新版为准，
    并发提交的新版不会夹在校验与写入之间（否则批注会挂到过期版本，
    而新版本缺少该批注的迁移记录，破坏逐版迁移记录的完整性）。
    校验失败则整体回滚，不留下批注或位置。
    """
    _get_document(con, document_id)
    with db.transaction(con):
        version = _latest_version(con, document_id)
        text = version["content"]

        if payload.end <= payload.start:
            raise HTTPException(422, "end 必须大于 start")
        if payload.end > len(text):
            raise HTTPException(
                422, f"end 超出文本长度（当前版本共 {len(text)} 个码点）"
            )
        actual = text[payload.start : payload.end]
        if actual != payload.selection:
            raise HTTPException(
                422,
                f"选区与偏移不符：text[{payload.start}:{payload.end}]={actual!r}，"
                f"而 selection={payload.selection!r}（偏移按 Unicode 码点计）",
            )
        if payload.left_context:
            left_start = max(0, payload.start - len(payload.left_context))
            if text[left_start : payload.start] != payload.left_context:
                raise HTTPException(422, "left_context 与选区左侧实际文本不符")
        if payload.right_context:
            if text[payload.end : payload.end + len(payload.right_context)] != (
                payload.right_context
            ):
                raise HTTPException(422, "right_context 与选区右侧实际文本不符")

        left_match = len(payload.left_context)
        right_match = len(payload.right_context)
        initial_candidate = {
            "start": payload.start,
            "end": payload.end,
            "left_match": left_match,
            "right_match": right_match,
            "score": left_match + right_match,
        }
        cur = con.execute(
            """
            INSERT INTO annotations
                (document_id, body, selection, left_context, right_context,
                 orig_start, orig_end, created_version_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                payload.body,
                payload.selection,
                payload.left_context,
                payload.right_context,
                payload.start,
                payload.end,
                version["id"],
            ),
        )
        annotation_id = cur.lastrowid
        cur = con.execute(
            """
            INSERT INTO locations
                (annotation_id, version_id, status, start, "end",
                 left_match, right_match, score, candidates)
            VALUES (?, ?, 'resolved', ?, ?, ?, ?, ?, ?)
            """,
            (
                annotation_id,
                version["id"],
                payload.start,
                payload.end,
                left_match,
                right_match,
                left_match + right_match,
                json.dumps([initial_candidate], ensure_ascii=False),
            ),
        )
        location_id = cur.lastrowid

    loc = con.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    ann = con.execute(
        "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
    ).fetchone()
    return {
        "annotation_id": annotation_id,
        "anchor": _annotation_out(con, ann)["anchor"],
        "location": _location_out(con, loc),
    }


@app.get("/documents/{document_id}/annotations")
def list_annotations(document_id: int, con=Depends(get_db)):
    """文档下全部批注及其在最新版上的生效状态。"""
    _get_document(con, document_id)
    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "annotations": [_annotation_out(con, ann) for ann in anns],
    }


@app.get("/documents/{document_id}/annotations/{annotation_id}")
def get_annotation(document_id: int, annotation_id: int, con=Depends(get_db)):
    _get_document(con, document_id)
    ann = _get_annotation(con, document_id, annotation_id)
    return _annotation_out(con, ann)


@app.get("/documents/{document_id}/annotations/{annotation_id}/history")
def annotation_history(document_id: int, annotation_id: int, con=Depends(get_db)):
    """一条批注跨全部版本的范围、状态与匹配依据（按版本升序）。"""
    _get_document(con, document_id)
    ann = _get_annotation(con, document_id, annotation_id)
    locs = con.execute(
        """
        SELECT l.* FROM locations l
        JOIN versions v ON v.id = l.version_id
        WHERE l.annotation_id = ?
        ORDER BY v.number
        """,
        (annotation_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "annotation_id": annotation_id,
        "body": ann["body"],
        "anchor": _annotation_out(con, ann)["anchor"],
        "history": [_location_out(con, loc) for loc in locs],
    }


@app.post(
    "/documents/{document_id}/annotations/{annotation_id}/confirmations",
    status_code=201,
)
def confirm_annotation(
    document_id: int,
    annotation_id: int,
    payload: ConfirmationCreate,
    con=Depends(get_db),
):
    """人工确认歧义批注：从候选范围中选定一个（只追加，确认后恢复自动迁移）。"""
    _get_document(con, document_id)
    _get_annotation(con, document_id, annotation_id)
    version = _get_version(con, document_id, payload.version_number)
    loc = con.execute(
        "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
        (annotation_id, version["id"]),
    ).fetchone()
    if loc is None:
        raise HTTPException(404, "该版本没有此批注的迁移记录")
    if loc["status"] != "ambiguous":
        raise HTTPException(409, f"该记录状态为 {loc['status']}，无需确认")
    if loc["carried_from"] is not None:
        raise HTTPException(
            409,
            "该记录是歧义顺延的占位记录，请确认原始歧义记录"
            f"（carried_from_location_id={loc['carried_from']}）",
        )
    if _confirmation_of(con, loc["id"]) is not None:
        raise HTTPException(409, "该歧义记录已确认过，确认结果只追加不可更改")

    candidates = json.loads(loc["candidates"])
    if not any(
        c["start"] == payload.start and c["end"] == payload.end for c in candidates
    ):
        raise HTTPException(
            422,
            "所选范围不在候选列表中，候选范围为："
            + json.dumps(
                [[c["start"], c["end"]] for c in candidates], ensure_ascii=False
            ),
        )

    with db.transaction(con):
        cur = con.execute(
            'INSERT INTO confirmations (location_id, start, "end", note) VALUES (?, ?, ?, ?)',
            (loc["id"], payload.start, payload.end, payload.note),
        )
        confirmation_id = cur.lastrowid
    return {
        "confirmation_id": confirmation_id,
        "location": _location_out(con, loc),
    }


# ---------------------------------------------------------------------------
# 任意两版对比
# ---------------------------------------------------------------------------


@app.get("/documents/{document_id}/compare")
def compare_versions(
    document_id: int,
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
    con=Depends(get_db),
):
    """任意两版之间，每条批注的范围、状态与匹配依据。"""
    _get_document(con, document_id)
    v_from = _get_version(con, document_id, from_version)
    v_to = _get_version(con, document_id, to_version)
    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    items = []
    for ann in anns:
        from_loc = con.execute(
            "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
            (ann["id"], v_from["id"]),
        ).fetchone()
        to_loc = con.execute(
            "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
            (ann["id"], v_to["id"]),
        ).fetchone()
        items.append(
            {
                "annotation_id": ann["id"],
                "body": ann["body"],
                "selection": ann["selection"],
                "from": _location_out(con, from_loc) if from_loc else None,
                "to": _location_out(con, to_loc) if to_loc else None,
            }
        )
    return {
        "document_id": document_id,
        "from_version": from_version,
        "to_version": to_version,
        "annotations": items,
    }


# ---------------------------------------------------------------------------
# 批注改动影响分析
# ---------------------------------------------------------------------------

_CLASSIFICATIONS = ("unchanged", "shifted", "modified", "deleted", "indeterminate")


@app.get("/documents/{document_id}/impact")
def impact_analysis(
    document_id: int,
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
    con=Depends(get_db),
):
    """批注改动影响分析：把起始版每条批注的范围投影到目标版并分类。

    - 基于两版全文生成稳定的码点级编辑片段（insert / delete / replace）；
    - 每条批注分类为 unchanged / shifted / modified / deleted / indeterminate，
      并返回原范围、投影范围与命中的编辑片段；
    - 起始版位置歧义、未解析或缺失时不猜测范围，返回 indeterminate 及原因；
    - 相同输入的片段顺序与分类必然相同（确定性计算，结果不落库）。
    """
    _get_document(con, document_id)
    v_from = _get_version(con, document_id, from_version)
    v_to = _get_version(con, document_id, to_version)

    segments = compute_segments(v_from["content"], v_to["content"])
    segment_dicts = [seg.to_dict(i) for i, seg in enumerate(segments)]

    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()

    summary = {key: 0 for key in _CLASSIFICATIONS}
    items = []
    for ann in anns:
        loc_row = con.execute(
            "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
            (ann["id"], v_from["id"]),
        ).fetchone()

        classification = "indeterminate"
        reason = None
        from_view = None
        to_view = None
        hits: list = []

        if loc_row is None:
            reason = (
                f"该批注在起始版第 {from_version} 版没有迁移记录"
                "（批注创建于该版之后），不提供投影范围"
            )
        else:
            loc = _location_out(con, loc_row)
            from_view = {
                "location_id": loc["location_id"],
                "status": loc["status"],
                "start": loc["start"],
                "end": loc["end"],
            }
            if loc["status"] in ("resolved", "confirmed"):
                start, end = loc["start"], loc["end"]
                proj_start, proj_end = project_range(segments, start, end)
                classification = classify_range(segments, start, end)
                to_view = {"start": proj_start, "end": proj_end}
                hits = [
                    segment_dicts[i]
                    for i in hit_segment_indices(segments, start, end)
                ]
            elif loc["status"] == "ambiguous":
                if loc["carried_forward"]:
                    reason = (
                        f"起始版第 {from_version} 版中该批注为歧义顺延占位记录"
                        "（原始歧义未经人工确认），不提供投影范围"
                    )
                else:
                    reason = (
                        f"起始版第 {from_version} 版中该批注位置歧义且未人工确认"
                        "（存在多个并列最高分的候选范围），不提供投影范围"
                    )
            else:  # unresolved
                reason = (
                    f"起始版第 {from_version} 版中该批注未解析"
                    "（选区在该版没有精确落点），不提供投影范围"
                )

        summary[classification] += 1
        items.append(
            {
                "annotation_id": ann["id"],
                "body": ann["body"],
                "selection": ann["selection"],
                "classification": classification,
                "reason": reason,
                "from": from_view,
                "to": to_view,
                "hits": hits,
            }
        )

    return {
        "document_id": document_id,
        "from_version": from_version,
        "to_version": to_version,
        "segment_count": len(segment_dicts),
        "segments": segment_dicts,
        "summary": summary,
        "annotations": items,
    }


# ---------------------------------------------------------------------------
# 批注驱动的脱敏副本
# ---------------------------------------------------------------------------


def _redaction_error(index: int, annotation_id: int, error: str, message: str) -> dict:
    return {
        "error": error,
        "message": message,
        "path": f"annotation_ids[{index}]",
        "annotation_id": annotation_id,
    }


@app.post("/documents/{document_id}/redact")
def create_redaction(
    document_id: int, payload: RedactionCreate, con=Depends(get_db)
):
    """生成批注驱动的脱敏副本（只读，不写入任何数据）。

    在**同一个 SQLite 读事务**（同一快照）内读取目标版本全文与每条批注在
    该版本上的生效位置；只接受生效状态为 ``resolved`` / ``confirmed`` 且
    范围有效的批注。以下情况整批拒绝（422，``detail.errors`` 一次性返回
    全部可定位错误），不猜测、不输出半成品：

    - 请求中重复出现的批注 ID（``duplicate_annotation``）；
    - 批注不存在（``annotation_not_found``）或属于其他文档
      （``cross_document``）；
    - 该版本上没有迁移记录（``location_missing``，如批注创建于更晚版本）；
    - 生效位置为 ``ambiguous``（含顺延占位）/ ``unresolved``
      （``not_effective``），或记录缺少有效范围（``invalid_range``）。

    通过的生效范围中，重叠或首尾相接的区间合并为最大区间，按起点顺序
    用同一个占位文本替换：每个占位只出现一次，并列出覆盖它的批注 ID。
    响应返回脱敏全文与源 / 结果 Unicode 码点半开区间映射
    （``fragments`` 同时覆盖遮盖与未遮盖片段，各自铺满全文）。
    相同快照与相同请求必然得到字节一致的结果。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        version = _get_version(con, document_id, payload.version_number)
        text = version["content"]

        errors: list = []
        unique_ids: set = set()
        # 重复 ID：每个出现位置都报告；先收集全部输入错误再读位置
        for index, annotation_id in enumerate(payload.annotation_ids):
            if annotation_id in unique_ids:
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "duplicate_annotation",
                        f"批注 {annotation_id} 在 annotation_ids 中重复出现",
                    )
                )
            unique_ids.add(annotation_id)

        ranges: list = []
        for annotation_id in sorted(unique_ids):
            ann = con.execute(
                "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
            ).fetchone()
            if ann is None:
                index = payload.annotation_ids.index(annotation_id)
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "annotation_not_found",
                        f"批注 {annotation_id} 不存在",
                    )
                )
                continue
            if ann["document_id"] != document_id:
                index = payload.annotation_ids.index(annotation_id)
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "cross_document",
                        f"跨文档引用：批注 {annotation_id} 属于文档 "
                        f"{ann['document_id']}，不能在文档 {document_id} 中脱敏",
                    )
                )
                continue

            loc = con.execute(
                "SELECT * FROM locations WHERE annotation_id = ? AND version_id = ?",
                (annotation_id, version["id"]),
            ).fetchone()
            index = payload.annotation_ids.index(annotation_id)
            if loc is None:
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "location_missing",
                        f"批注 {annotation_id} 在第 {payload.version_number} 版"
                        "没有迁移记录（该批注创建于更晚版本），无法脱敏",
                    )
                )
                continue

            conf = _confirmation_of(con, loc["id"])
            if conf is not None:
                status, start, end = "confirmed", conf["start"], conf["end"]
            else:
                status, start, end = loc["status"], loc["start"], loc["end"]
            if status not in ("resolved", "confirmed"):
                if status == "ambiguous" and loc["carried_from"] is not None:
                    detail = "位置为歧义顺延占位（原始歧义未经人工确认）"
                elif status == "ambiguous":
                    detail = "位置歧义且未经人工确认（存在多个并列最高分的候选范围）"
                else:
                    detail = "位置未解析（选区在该版没有精确落点）"
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "not_effective",
                        f"批注 {annotation_id} 在第 {payload.version_number} 版的"
                        f"生效状态为 {status}：{detail}，不提供脱敏范围",
                    )
                )
                continue
            if (
                start is None
                or end is None
                or not (0 <= start < end <= len(text))
            ):
                errors.append(
                    _redaction_error(
                        index,
                        annotation_id,
                        "invalid_range",
                        f"批注 {annotation_id} 在第 {payload.version_number} 版的"
                        f"生效范围 [{start}, {end}) 无效"
                        f"（文本长度 {len(text)} 个码点）",
                    )
                )
                continue
            ranges.append(EffectiveRange(annotation_id, start, end))

        if errors:
            # 稳定排序：按请求位置，其次按错误码
            errors.sort(key=lambda e: (e["path"], e["error"]))
            raise HTTPException(
                422,
                detail={
                    "message": "脱敏请求校验失败，整批未处理（未生成脱敏副本）",
                    "errors": errors,
                },
            )

        intervals = merge_ranges(ranges)
        result = build_redaction(text, intervals, payload.placeholder)

    return {
        "document_id": document_id,
        "version_number": payload.version_number,
        "placeholder": payload.placeholder,
        "annotation_ids": sorted(unique_ids),
        "source_length": len(text),
        "redacted": result["redacted"],
        "redacted_interval_count": len(intervals),
        "fragments": result["fragments"],
        "placeholders": result["placeholders"],
    }


# ---------------------------------------------------------------------------
# 审阅依赖编排
# ---------------------------------------------------------------------------


def _dependency_out(row: sqlite3.Row) -> dict:
    return {
        "dependency_id": row["id"],
        "prerequisite_id": row["prerequisite_id"],
        "dependent_id": row["dependent_id"],
        "created_at": row["created_at"],
    }


def _completion_out(row: sqlite3.Row, already_completed: bool, unlocked: list) -> dict:
    return {
        "completion_id": row["id"],
        "document_id": row["document_id"],
        "annotation_id": row["annotation_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "already_completed": already_completed,
        "unlocked_annotation_ids": unlocked,
    }


def _review_state(con: sqlite3.Connection, document_id: int):
    """文档当前的审阅状态：可处理 / 被阻塞 / 已完成 三类的批注编号集合。

    被阻塞 = 未完成且存在尚未完成的传递前置项；可处理 = 未完成且无阻塞。
    """
    ann_ids = [
        r["id"]
        for r in con.execute(
            "SELECT id FROM annotations WHERE document_id = ?", (document_id,)
        ).fetchall()
    ]
    edges = [
        (r["prerequisite_id"], r["dependent_id"])
        for r in con.execute(
            "SELECT prerequisite_id, dependent_id FROM dependencies WHERE document_id = ?",
            (document_id,),
        ).fetchall()
    ]
    completed = {
        r["annotation_id"]
        for r in con.execute(
            "SELECT annotation_id FROM completions WHERE document_id = ?",
            (document_id,),
        ).fetchall()
    }
    prereqs = build_prereq_map(edges)
    actionable, blocked = set(), set()
    for aid in ann_ids:
        if aid in completed:
            continue
        incomplete = [
            a for a in transitive_prerequisites(prereqs, aid) if a not in completed
        ]
        (blocked if incomplete else actionable).add(aid)
    return ann_ids, prereqs, completed, actionable, blocked


@app.post("/documents/{document_id}/dependencies", status_code=201)
def create_dependencies(
    document_id: int, payload: DependencyBatchCreate, con=Depends(get_db)
):
    """批量提交同一文档内「前置批注 → 后续批注」的审阅依赖关系。

    整批写入前依次校验（任一失败则全部不落库）：
    跨文档引用（422）→ 自依赖（422）→ 批内/已存重复边（422/409）→ 环（422）。
    发现环时按批注编号升序遍历，返回首条以环中最小编号开头的闭环路径。
    """
    _get_document(con, document_id)
    relations = payload.relations
    with db.transaction(con):
        # 1. 引用校验：批注必须存在且属于当前文档（禁止跨文档引用）
        for rel in relations:
            for aid in (rel.prerequisite_id, rel.dependent_id):
                row = con.execute(
                    "SELECT id, document_id FROM annotations WHERE id = ?", (aid,)
                ).fetchone()
                if row is None:
                    raise HTTPException(404, f"批注 {aid} 不存在")
                if row["document_id"] != document_id:
                    raise HTTPException(
                        422,
                        f"跨文档引用：批注 {aid} 属于文档 {row['document_id']}，"
                        f"不能在文档 {document_id} 中建立依赖",
                    )
        # 2. 自依赖
        for rel in relations:
            if rel.prerequisite_id == rel.dependent_id:
                raise HTTPException(
                    422, f"自依赖：批注 {rel.prerequisite_id} 不能依赖自身"
                )
        # 3. 批内重复边
        seen = set()
        for rel in relations:
            edge = (rel.prerequisite_id, rel.dependent_id)
            if edge in seen:
                raise HTTPException(
                    422,
                    f"重复边：前置批注 {edge[0]} → 后续批注 {edge[1]} "
                    "在本批中出现多次",
                )
            seen.add(edge)
        # 4. 与已落库边重复
        existing_edges = [
            (r["prerequisite_id"], r["dependent_id"])
            for r in con.execute(
                "SELECT prerequisite_id, dependent_id FROM dependencies "
                "WHERE document_id = ?",
                (document_id,),
            ).fetchall()
        ]
        existing_set = set(existing_edges)
        for edge in seen:
            if edge in existing_set:
                raise HTTPException(
                    409,
                    f"重复边：前置批注 {edge[0]} → 后续批注 {edge[1]} 已存在",
                )
        # 5. 环检测（已落库边 ∪ 本批新边）
        cycle = find_cycle_path(existing_edges + sorted(seen))
        if cycle is not None:
            raise HTTPException(
                422,
                detail={
                    "message": "依赖关系存在环："
                    + " → ".join(str(n) for n in cycle),
                    "cycle": cycle,
                },
            )
        # 6. 全部校验通过，整批写入
        created = []
        for rel in relations:
            cur = con.execute(
                "INSERT INTO dependencies (document_id, prerequisite_id, dependent_id)"
                " VALUES (?, ?, ?)",
                (document_id, rel.prerequisite_id, rel.dependent_id),
            )
            row = con.execute(
                "SELECT * FROM dependencies WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            created.append(_dependency_out(row))
    return {
        "document_id": document_id,
        "created_count": len(created),
        "dependencies": created,
    }


@app.get("/documents/{document_id}/dependencies")
def list_dependencies(document_id: int, con=Depends(get_db)):
    """文档下全部审阅依赖边（按写入顺序）。"""
    _get_document(con, document_id)
    rows = con.execute(
        "SELECT * FROM dependencies WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "dependencies": [_dependency_out(r) for r in rows],
    }


@app.get("/documents/{document_id}/review-plan")
def review_plan(document_id: int, con=Depends(get_db)):
    """审阅计划：按依赖图与完成记录划分可处理 / 被阻塞 / 已完成。

    - 被阻塞项列出所有尚未完成的传递前置项（按编号升序）；
    - 未完成批注按拓扑层级划分为可并行批次（batches）：
      第 0 层即可处理项，第 k 层在其前置全部落入更低层后可并行处理；
    - 只读的确定性计算，结果不落库。
    """
    _get_document(con, document_id)
    anns = con.execute(
        "SELECT * FROM annotations WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    ann_ids, prereqs, completed, _, _ = _review_state(con, document_id)
    completion_rows = {
        r["annotation_id"]: r
        for r in con.execute(
            "SELECT * FROM completions WHERE document_id = ?", (document_id,)
        ).fetchall()
    }

    actionable, blocked, done = [], [], []
    for ann in anns:
        aid = ann["id"]
        comp = completion_rows.get(aid)
        if comp is not None:
            done.append(
                {
                    "annotation_id": aid,
                    "body": ann["body"],
                    "note": comp["note"],
                    "completed_at": comp["created_at"],
                }
            )
            continue
        item = {
            "annotation_id": aid,
            "body": ann["body"],
            "prerequisite_ids": sorted(prereqs.get(aid, ())),
        }
        incomplete = sorted(
            a for a in transitive_prerequisites(prereqs, aid) if a not in completed
        )
        if incomplete:
            item["incomplete_prerequisites"] = incomplete
            blocked.append(item)
        else:
            actionable.append(item)

    waves = topological_waves(ann_ids, prereqs, completed)
    return {
        "document_id": document_id,
        "summary": {
            "total": len(anns),
            "completed": len(done),
            "actionable": len(actionable),
            "blocked": len(blocked),
            "batch_count": len(waves),
        },
        "actionable": actionable,
        "blocked": blocked,
        "completed": done,
        "batches": [
            {"level": level, "annotation_ids": wave}
            for level, wave in enumerate(waves)
        ],
    }


@app.post(
    "/documents/{document_id}/annotations/{annotation_id}/completions",
    status_code=201,
)
def create_completion(
    document_id: int,
    annotation_id: int,
    payload: CompletionCreate,
    response: Response,
    con=Depends(get_db),
):
    """标记批注审阅完成（只追加，重复提交幂等返回 200 与原记录）。

    - 被阻塞（存在未完成传递前置项）的批注不可完成（409）；
    - 完成前置批注后立即解锁符合条件的后续批注，
      响应中的 unlocked_annotation_ids 列出本次新解锁的批注。
    """
    _get_document(con, document_id)
    _get_annotation(con, document_id, annotation_id)
    with db.transaction(con):
        existing = con.execute(
            "SELECT * FROM completions WHERE annotation_id = ?", (annotation_id,)
        ).fetchone()
        if existing is not None:
            # 幂等：重复提交返回原记录，不新增行、不改变状态
            response.status_code = 200
            return _completion_out(existing, already_completed=True, unlocked=[])

        _, prereqs, completed, _, blocked = _review_state(con, document_id)
        if annotation_id in blocked:
            incomplete = sorted(
                a
                for a in transitive_prerequisites(prereqs, annotation_id)
                if a not in completed
            )
            raise HTTPException(
                409,
                detail={
                    "message": f"批注 {annotation_id} 被阻塞："
                    "存在尚未完成的前置批注",
                    "incomplete_prerequisites": incomplete,
                },
            )

        cur = con.execute(
            "INSERT INTO completions (document_id, annotation_id, note)"
            " VALUES (?, ?, ?)",
            (document_id, annotation_id, payload.note),
        )
        row = con.execute(
            "SELECT * FROM completions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        # 立即解锁：完成前被阻塞、完成后可处理的批注
        _, _, _, actionable_after, _ = _review_state(con, document_id)
        unlocked = sorted(aid for aid in blocked if aid in actionable_after)
    return _completion_out(row, already_completed=False, unlocked=unlocked)


@app.get("/documents/{document_id}/completions")
def list_completions(document_id: int, con=Depends(get_db)):
    """文档下全部完成记录（只追加，按写入顺序）。"""
    _get_document(con, document_id)
    rows = con.execute(
        "SELECT * FROM completions WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "completions": [
            {
                "completion_id": r["id"],
                "annotation_id": r["annotation_id"],
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 迁移归档：规范导出与离线核验
# ---------------------------------------------------------------------------

_DIGEST_DESCRIPTION = (
    "摘要算法：sha256(json.dumps(archive, ensure_ascii=False, "
    "sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()"
)


@app.get("/documents/{document_id}/archive")
def export_archive(document_id: int, con=Depends(get_db)):
    """导出指定文档的迁移归档及其 SHA-256 摘要。

    文档元数据、全部版本、批注、迁移记录与人工确认在**同一个 SQLite 读事务**
    （同一快照）中取出；各列表按行 ID 升序、字段固定，归档内容规范化后
    计算 SHA-256 摘要。摘要应在导出后另行保存，与归档分开存放或转交。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        arch = archive_mod.build_archive(con, document_id)
    digest = archive_mod.compute_digest(arch)
    return {
        "document_id": document_id,
        "format": archive_mod.ARCHIVE_FORMAT,
        "digest": digest,
        "digest_algorithm": _DIGEST_DESCRIPTION,
        "archive": arch,
    }


@app.post("/archives/verify")
def verify_archive_endpoint(payload: ArchiveVerifyRequest):
    """离线核验迁移归档：复算摘要并检查内部一致性。

    **不查当前数据库、不写入任何数据**——只依据请求中的归档与摘要：

    1. 复算摘要（摘要不符或归档被篡改 → ``digest_mismatch``）；
    2. 版本编号必须从 1 开始连续无重复；
    3. 版本、批注归属归档文档（防串文档），迁移记录的批注 / 版本引用、
       确认的迁移记录引用、批注的创建版引用均须存在；
    4. 每条批注从创建版到末版，每版恰有一条迁移记录；
    5. 顺延引用（``carried_from``）须指向同一批注较早版本的真实歧义记录
       （非顺延、``status=ambiguous``）；
    6. 人工确认须指向非顺延的歧义记录，且所选范围属于其候选落点。

    任一失败返回 422，``detail.errors`` 列出全部可定位错误
    （``error`` 错误码、``message`` 业务说明、``path`` JSON 路径定位）。
    """
    arch = payload.archive.model_dump(mode="json")
    errors = archive_mod.verify_archive(arch, payload.digest)
    if errors:
        raise HTTPException(
            422,
            detail={"valid": False, "errors": [e.to_dict() for e in errors]},
        )
    return {
        "valid": True,
        "format": arch["format"],
        "document_id": arch["document"]["id"],
        "digest": payload.digest.strip().lower(),
        "counts": {
            "versions": len(arch["versions"]),
            "annotations": len(arch["annotations"]),
            "locations": len(arch["locations"]),
            "confirmations": len(arch["confirmations"]),
        },
    }


# ---------------------------------------------------------------------------
# 文档发布门禁：策略修订（按名称只追加）与同事务快照评估
# ---------------------------------------------------------------------------


def _policy_revision_out(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "revision": row["revision"],
        "strategy": json.loads(row["strategy_json"]),
        "created_at": row["created_at"],
    }


def _get_policy_revision(
    con: sqlite3.Connection, name: str, revision: Optional[int] = None
) -> sqlite3.Row:
    """取指定名称的某一策略修订；``revision`` 为 None 时取最新修订。"""
    if revision is None:
        row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? "
            "ORDER BY revision DESC LIMIT 1",
            (name,),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? AND revision = ?",
            (name, revision),
        ).fetchone()
    if row is None:
        if revision is None:
            raise HTTPException(404, f"门禁策略 {name!r} 不存在（尚无任何修订）")
        raise HTTPException(
            404, f"门禁策略 {name!r} 不存在第 {revision} 版修订"
        )
    return row


def _gate_snapshot(con: sqlite3.Connection, document_id: int, version_id: int):
    """在读事务快照内汇总门禁评估输入：目标版位置状态 + 当前审阅状态。

    位置状态取目标版本上每条批注迁移记录的**生效**状态（有人工确认时为
    confirmed；顺延占位仍是 ambiguous；该版本无迁移记录的批注不计入任何
    位置状态）；审阅状态按当前依赖图与完成记录划分。所有 SELECT 必须由
    调用方包在同一个读事务中。
    """
    loc_rows = con.execute(
        """
        SELECT l.annotation_id, l.status,
               EXISTS (SELECT 1 FROM confirmations c WHERE c.location_id = l.id)
                   AS has_confirmation
        FROM locations l
        JOIN annotations a ON a.id = l.annotation_id
        WHERE l.version_id = ? AND a.document_id = ?
        """,
        (version_id, document_id),
    ).fetchall()
    location_ids = {status: set() for status in gate_mod.LOCATION_STATUSES}
    for row in loc_rows:
        status = "confirmed" if row["has_confirmation"] else row["status"]
        location_ids[status].add(row["annotation_id"])

    ann_ids, _, completed, actionable, blocked = _review_state(con, document_id)
    review_ids = {
        "actionable": set(actionable),
        "blocked": set(blocked),
        "completed": set(completed),
    }
    # 防御：审阅状态以文档全部批注为全集
    assert actionable | blocked | completed == set(ann_ids)
    return gate_mod.GateSnapshot(
        location_ids_by_status=location_ids,
        review_ids_by_status=review_ids,
    )


@app.post("/policies", status_code=201)
def create_policy(payload: PolicyCreate, con=Depends(get_db)):
    """提交文档发布门禁策略：语法校验通过后**按名称只追加**一个修订。

    - 策略节点只允许 all / any / not 与 location_count / review_count 叶子，
      叶子字段固定为 status、op、value；
    - 未知字段、空组合、not 子项不是一个、深度超过 8 或节点超过 64 时
      **整条拒绝**（422，``detail.errors`` 带 JSON 路径定位，不落任何修订）；
    - 同名重复提交一律生成递增的新修订（revision 从 1 起），
      旧修订不修改、不删除；策略树按提交原样保存（子节点顺序即提交顺序）。
    """
    errors = gate_mod.validate_strategy(payload.strategy)
    if errors:
        raise HTTPException(
            422,
            detail={
                "message": f"门禁策略 {payload.name!r} 校验失败，本次修订未落库",
                "errors": [e.to_dict() for e in errors],
            },
        )
    strategy_json = json.dumps(payload.strategy, ensure_ascii=False)
    with db.transaction(con):
        revision = con.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS r "
            "FROM policy_revisions WHERE name = ?",
            (payload.name,),
        ).fetchone()["r"]
        con.execute(
            "INSERT INTO policy_revisions (name, revision, strategy_json) "
            "VALUES (?, ?, ?)",
            (payload.name, revision, strategy_json),
        )
        row = con.execute(
            "SELECT * FROM policy_revisions WHERE name = ? AND revision = ?",
            (payload.name, revision),
        ).fetchone()
    return _policy_revision_out(row)


@app.get("/policies")
def list_policies(con=Depends(get_db)):
    """列出全部门禁策略的最新修订（按名称升序；修订历史按名称另查）。"""
    rows = con.execute(
        """
        SELECT p.* FROM policy_revisions p
        JOIN (SELECT name, MAX(revision) AS revision
              FROM policy_revisions GROUP BY name) AS latest
          ON latest.name = p.name AND latest.revision = p.revision
        ORDER BY p.name
        """
    ).fetchall()
    return {"policies": [_policy_revision_out(row) for row in rows]}


@app.get("/policies/{name}")
def list_policy_revisions(name: str, con=Depends(get_db)):
    """某名称门禁策略的全部修订（只追加，按修订号升序）。"""
    _get_policy_revision(con, name)
    rows = con.execute(
        "SELECT * FROM policy_revisions WHERE name = ? ORDER BY revision",
        (name,),
    ).fetchall()
    return {"name": name, "revisions": [_policy_revision_out(row) for row in rows]}


@app.get("/policies/{name}/revisions/{revision}")
def get_policy_revision(name: str, revision: int, con=Depends(get_db)):
    """取某名称门禁策略的指定修订（审计用）。"""
    return _policy_revision_out(_get_policy_revision(con, name, revision))


@app.get("/documents/{document_id}/gate/evaluate")
def evaluate_gate(
    document_id: int,
    policy: str = Query(..., description="门禁策略名称"),
    revision: Optional[int] = Query(None, ge=1, description="策略修订号；缺省取最新"),
    version: Optional[int] = Query(
        None, ge=1, description="目标文档版本号；缺省取最新版本"
    ),
    con=Depends(get_db),
):
    """在同一快照上评估发布门禁策略，返回同形解释树。

    策略修订、目标文档版本、目标版全部批注的生效位置状态与当前审阅状态
    （actionable / blocked / completed）在**同一个 SQLite 读事务**内读取，
    评估期间不会混入并发写入。解释树：

    - 与策略树同形，``all`` / ``any`` **不短路**（每个子节点都被评估）；
    - 叶子列出 ``actual``（实际计数）、``value``（阈值）、``op`` 与
      参与计数的批注 ID（``annotation_ids``，按编号升序）；
    - 每个节点带布尔 ``result``，根节点结果即整体门禁结论 ``passed``；
    - 相同快照（同一修订、同一版本、同一审阅状态）下结果与 ID 顺序稳定。
    """
    with db.read_transaction(con):
        _get_document(con, document_id)
        policy_row = _get_policy_revision(con, policy, revision)
        if version is None:
            version_row = _latest_version(con, document_id)
            if version_row is None:
                raise HTTPException(404, f"文档 {document_id} 没有任何版本")
        else:
            version_row = _get_version(con, document_id, version)
        strategy = json.loads(policy_row["strategy_json"])
        snapshot = _gate_snapshot(con, document_id, version_row["id"])
        tree = gate_mod.evaluate_strategy(strategy, snapshot)
    return {
        "document_id": document_id,
        "policy_name": policy_row["name"],
        "policy_revision": policy_row["revision"],
        "version_number": version_row["number"],
        "passed": tree["result"],
        "explanation": tree,
    }
