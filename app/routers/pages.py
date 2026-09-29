from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.services.vat_rules import VatRuleError, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}

# 近笔窗口与折线窗口共用：折线点 = 近笔窗口里带电位的读数
RECENT_LOT_LIMIT = 5
SPARK_WIDTH = 72
SPARK_HEIGHT = 28


class PayloadConsistencyError(RuntimeError):
    """折线序列 / 近笔列表 / 状态角标口径对不上 —— 不允许静默下发，本路直接判失败。"""


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _lot_sort_key(lot: DipLot):
    # 全页与局部唯一排序键：浸染时间为主、批次主键兜底（同一时刻也稳定）。
    # 种子数据带时区、表单录入为本地无时区时间，统一按 UTC 比较，避免混排抛 TypeError。
    ts = lot.dippedAt
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (ts.astimezone(timezone.utc), lot.id)


def _ordered_lots(vat: Vat) -> list[DipLot]:
    return sorted(vat.lots, key=_lot_sort_key)


def _spark_points(values: list[float]) -> list[dict]:
    """由近笔窗口中带电位的读数序列派生 sparkline 坐标（无读数则空）。

    坐标只在这里、由真实批次读数计算；页面永远拿到算好的点，不存在写死折线点一说。
    """
    vals = [float(v) for v in values]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0.0 if n == 1 else round(i * (SPARK_WIDTH - 1) / (n - 1), 2)
        y = round(SPARK_HEIGHT - 1 - ((v - lo) / span) * (SPARK_HEIGHT - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _lot_payload(lot: DipLot) -> dict:
    return {
        "id": lot.id,
        "dippedAt": lot.dippedAt.strftime("%Y-%m-%d %H:%M"),
        "clothMeters": float(lot.clothMeters),
        "redoxMv": float(lot.redoxMv) if lot.redoxMv is not None else None,
    }


def _vat_payload(vat: Vat) -> dict:
    # 同一缸主键（vat.id）取数；近笔与折线从同一有序批次序列、同一窗口派生。
    lots_asc = _ordered_lots(vat)
    recent_asc = lots_asc[-RECENT_LOT_LIMIT:]          # 同一窗口（远 → 近）
    recent = list(reversed(recent_asc))                # 近笔展示：近 → 远
    redox_series = [float(l.redoxMv) for l in recent_asc if l.redoxMv is not None]
    spark = _spark_points(redox_series)                # 折线：远 → 近正序
    latest = lots_asc[-1] if lots_asc else None

    # —— 口径自检：折线上的每个点必须与近笔里带电位的记录一一对应 ——
    redox_in_recent = [l for l in recent if l.redoxMv is not None]
    if len(spark) != len(redox_in_recent):
        raise PayloadConsistencyError(
            f"缸 {vat.id} 折线点数 {len(spark)} 与近笔带电位条数 "
            f"{len(redox_in_recent)} 不一致"
        )
    # 折线值序（远→近）必须等于近笔展示序（近→远）的反转，杜绝两路各排各的
    if list(reversed([float(l.redoxMv) for l in redox_in_recent])) != redox_series:
        raise PayloadConsistencyError(f"缸 {vat.id} 折线序列与近笔电位序列对不上")
    # 最近一条必须同时是近笔列表首条与「最近电位」来源
    if lots_asc and recent[0].id != latest.id:
        raise PayloadConsistencyError(f"缸 {vat.id} 最近批次与近笔首条对不上")
    # 角标必须等于库里的当前状态（不再有任何旁路缓存）
    if vat.status not in STATUS_LABELS:
        raise PayloadConsistencyError(f"缸 {vat.id} 状态非法：{vat.status!r}")

    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS[vat.status],
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "spark": spark,
        "recentLots": [_lot_payload(l) for l in recent],
    }


def _load_vats(db: Session) -> list[Vat]:
    # 缸位条按缸主键稳定排序；工坊关系与批次一次性 joinedload，杜绝局部路懒加载出旧序/空集。
    return (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.id)
        .all()
    )


def _load_vat(db: Session, pk: int) -> Optional[Vat]:
    return (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.id).all()
    vats = _load_vats(db)
    payloads = [_vat_payload(v) for v in vats]

    # 展开区指定的缸必须存在于下发数据中，否则前端会开一个空白面板 —— 直接判失败。
    if selected_vat is not None and not any(v["id"] == selected_vat for v in payloads):
        raise PayloadConsistencyError(f"展开缸 {selected_vat} 不在缸位数据中")

    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": payloads,
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


def _parse_lot_form(dipped_at: str, cloth_meters: str, redox_mv: str):
    """表单录入解析；任何不合规都抛 ValueError，由路由带着还原台页面一起返回（不留空白）。"""
    if not dipped_at.strip():
        raise ValueError("浸染时间未填")
    if not cloth_meters.strip():
        raise ValueError("布料米数未填")
    try:
        ts = datetime.fromisoformat(dipped_at)
    except ValueError:
        raise ValueError("浸染时间格式无效")
    if ts.tzinfo is None:
        # datetime-local 给的是本地墙钟时间，统一存为 UTC，保证后续与历史批次同键排序
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        meters = Decimal(cloth_meters)
    except InvalidOperation:
        raise ValueError("布料米数不是有效数字")
    redox = None
    if redox_mv.strip():
        try:
            redox = Decimal(redox_mv)
        except InvalidOperation:
            raise ValueError("氧化还原电位不是有效数字")
    return ts, meters, redox


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    try:
        ws = int(workshop) if workshop.strip() else None
    except ValueError:
        ws = None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    # 被拒：复用整页同一口径重新出数据，展开区保持打开，还原台不空白
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(""),
    clothMeters: str = Form(""),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    try:
        ws = int(workshop) if workshop.strip() else None
    except ValueError:
        ws = None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        ts, meters, redox = _parse_lot_form(dippedAt, clothMeters, redoxMv)
        lot = DipLot(
            vat_id=pk,
            dippedAt=ts,
            clothMeters=meters,
            redoxMv=redox,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except ValueError as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    # 被拒：复用整页同一口径重新出数据，展开区保持打开，还原台不空白
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.get("/bay/partial/{pk}", response_class=HTMLResponse)
async def bay_partial_vat(
    pk: int,
    request: Request,
    workshop: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """局部刷新：与整页完全同口径 —— 同缸主键、同排序键、同窗口、同截取条数。"""
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    if not item:
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, workshop, pk),
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
