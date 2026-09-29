from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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

# 缸位条折线与展开区近笔共用的唯一窗口大小（最近 N 笔浸染）。
RECENT_LIMIT = 5
SPARK_WIDTH = 72
SPARK_HEIGHT = 28


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = SPARK_WIDTH,
                  height: int = SPARK_HEIGHT) -> list[dict]:
    """把同窗口内的 redox 序列（旧 → 新）压成 sparkline 坐标，无有效读数则空。

    入参顺序必须来自 Vat.ordered_lots()，调用方不得另排序、另截断。
    """
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _assert_same_basis(payload: dict) -> None:
    """不变式守卫：折线点数必须等于展开区近笔里带电位的条数。

    整页与局部任一渲染路径产出对不上，直接判失败，不允许把不一致画到页面。
    """
    n_spark = len(payload["spark"])
    n_redox = sum(1 for l in payload["recentLots"] if l["redoxMv"] is not None)
    if n_spark != n_redox:
        raise RuntimeError(
            f"缸 {payload['code']} 口径不一致：折线 {n_spark} 点，"
            f"近笔带电位 {n_redox} 条"
        )


def _vat_payload(vat: Vat) -> dict:
    """同一缸主键、同一排序键、同一窗口产出的卡片数据。

    整页渲染与局部刷新都只能调这里，禁止在别处另算折线或近笔。
    """
    # 唯一排序键 (dippedAt, id)，旧 → 新
    lots = vat.ordered_lots()
    # 唯一窗口：最近 RECENT_LIMIT 笔（旧 → 新）；折线与近笔都取自它
    window = lots[-RECENT_LIMIT:]
    recent = list(reversed(window))  # 列表展示：新 → 旧
    latest = lots[-1] if lots else None

    payload = {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        # 角标文案永远跟随当前库内状态，不缓存、不另算
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        # 折线：同一窗口内带电位的批次，保持旧 → 新
        "spark": _spark_points([l for l in window if l.redoxMv is not None]),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }
    _assert_same_basis(payload)
    return payload


def _load_vat(db: Session, pk: int) -> Optional[Vat]:
    """局部与写路径共用的取数：同一缸主键，显式带出工坊与批次。"""
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
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


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
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    ws = int(workshop) if workshop.strip() else None
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
    # 被拒也必须回完整还原台（含本缸展开区与错误原因），不得空白
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
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        lot = DipLot(
            vat_id=pk,
            dippedAt=datetime.fromisoformat(dippedAt),
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.get("/bay/partial/{pk}")
async def bay_partial_vat(pk: int, request: Request, db: Session = Depends(get_db)):
    """局部刷新单缸：与整页同一 _vat_payload（同一缸、同一排序键、同一窗口）。

    只回这一缸的 JSON，前端原位替换；不重渲染整页、不另算排序。
    """
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = _load_vat(db, pk)
    if not item:
        return JSONResponse({"error": "vat_not_found"}, status_code=404)
    # _vat_payload 内部带口径守卫，对不上会直接 500，绝不返回两套数
    return JSONResponse(_vat_payload(item))


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
