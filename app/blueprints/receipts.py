from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Plant, Pond, WeighReceipt
from app.services.rules import latest_batch_for_pond, latest_valid_receipt_for_pond

bp = Blueprint("receipts", __name__, url_prefix="/receipts")

STATUS_LABELS = {
    Pond.STATUS_FILLING: "注水中",
    Pond.STATUS_SLAKING: "熟化中",
    Pond.STATUS_DRAWN: "已出灰",
}


def _parse_dt(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@bp.route("/")
@login_required
def list_receipts():
    plants = Plant.query.order_by(Plant.name).all()
    ponds = Pond.query.order_by(Pond.code).all()

    query = WeighReceipt.query.join(Pond)

    pond_id_raw = request.args.get("pond_id", "").strip()
    pond_id = int(pond_id_raw) if pond_id_raw.isdigit() else None
    if pond_id is not None:
        query = query.filter(WeighReceipt.pond_id == pond_id)

    plant_id_raw = request.args.get("plant_id", "").strip()
    if plant_id_raw.isdigit():
        query = query.filter(Pond.plant_id == int(plant_id_raw))

    scope = request.args.get("scope", "active")
    if scope == "active":
        query = query.filter(WeighReceipt.voided_at.is_(None))
    elif scope == "void":
        query = query.filter(WeighReceipt.voided_at.is_not(None))

    receipts = query.order_by(WeighReceipt.weighed_at.desc(), WeighReceipt.id.desc()).all()

    selected_pond = db.session.get(Pond, pond_id) if pond_id is not None else None
    return render_template(
        "receipts/list.html",
        receipts=receipts,
        plants=plants,
        ponds=ponds,
        status_labels=STATUS_LABELS,
        scope=scope,
        plant_id=int(plant_id_raw) if plant_id_raw.isdigit() else None,
        pond_id=pond_id,
        selected_pond=selected_pond,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create_receipt():
    # 只对已出灰池交单；空池（尚未出灰）不出现在选择里。
    ponds = (
        Pond.query.filter_by(status=Pond.STATUS_DRAWN)
        .order_by(Pond.code)
        .all()
    )

    if request.method == "POST":
        pond_id_raw = (request.form.get("pond_id") or "").strip()
        weight_raw = (request.form.get("net_weight_t") or "").strip()
        weighed_raw = (request.form.get("weighed_at") or "").strip()
        weigher = (request.form.get("weigher") or "").strip()

        errors = []
        pond = None
        if pond_id_raw.isdigit():
            pond = db.session.get(Pond, int(pond_id_raw))
        if pond is None:
            errors.append("请选择熟化池")
        elif pond.status != Pond.STATUS_DRAWN:
            errors.append("只能对本轮已出灰的熟化池交称重回执")

        try:
            weight = float(weight_raw)
            if not weight > 0:
                raise ValueError
        except ValueError:
            weight = None
            errors.append("净重吨必须为大于 0 的数字")

        try:
            weighed_at = _parse_dt(weighed_raw)
            if weighed_at is None:
                errors.append("请填写过磅时刻")
        except ValueError:
            weighed_at = None
            errors.append("过磅时刻格式无效")

        if not weigher:
            errors.append("请填写司磅人")

        batch = latest_batch_for_pond(pond) if pond else None
        if pond is not None and batch is None:
            errors.append("该池本轮没有熟化批次，不能交回执")

        if pond is not None and batch is not None:
            if latest_valid_receipt_for_pond(pond) is not None:
                errors.append("本池本轮已存在一张未作废称重回执，不能重复交单")

        if errors:
            for msg in errors:
                flash(msg, "error")
        else:
            receipt = WeighReceipt(
                pond_id=pond.id,
                batch_id=batch.id,
                net_weight_t=weight,
                weighed_at=weighed_at,
                weigher=weigher,
            )
            db.session.add(receipt)
            try:
                db.session.commit()
            except IntegrityError:
                # 两名司磅抢交同一池未作废回执：数据库部分唯一索引兜底。
                db.session.rollback()
                flash("本池本轮已有一张未作废称重回执，您这张未落库", "error")
                return redirect(url_for("receipts.list_receipts", pond_id=pond.id))
            flash(f"{pond.code} 称重回执已交（净重 {weight:g} 吨）", "ok")
            return redirect(
                url_for(
                    "board.floor_plan",
                    plant_id=pond.plant_id,
                    pond=pond.id,
                )
            )

    preselect_pond_id = (
        request.form.get("pond_id", type=int)
        if request.method == "POST"
        else request.args.get("pond_id", type=int)
    )
    return render_template(
        "receipts/form.html",
        ponds=ponds,
        weigher_default=current_user.username,
        preselect_pond_id=preselect_pond_id,
    )


@bp.route("/<int:receipt_id>/void", methods=["POST"])
@login_required
def void_receipt(receipt_id: int):
    if current_user.role != "admin":
        flash("只有管理员可以作废称重回执", "error")
        return redirect(url_for("receipts.list_receipts"))

    receipt = db.session.get(WeighReceipt, receipt_id) or abort(404)
    if receipt.voided_at is None:
        receipt.voided_at = datetime.now(timezone.utc)
        db.session.commit()
        flash(
            f"{receipt.pond.code} 的称重回执已作废，此后不得再凭它放行",
            "ok",
        )
    else:
        flash("该回执已作废，无需重复操作", "error")
    return redirect(
        url_for("receipts.list_receipts", pond_id=receipt.pond_id, scope="all")
    )
