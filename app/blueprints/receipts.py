"""出灰称重回执专页：可筛选、新建、作废。"""

from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Plant, Pond, SlakeBatch, WeighReceipt
from app.services.rules import active_receipt_for_round, latest_batch_for_pond

bp = Blueprint("receipts", __name__, url_prefix="/receipts")


def _parse_weighed_at(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@bp.route("/")
@login_required
def list_receipts():
    plant_id = request.args.get("plant_id", type=int)
    pond_id = request.args.get("pond_id", type=int)
    scope = request.args.get("scope", "active")  # active | void | all

    query = (
        WeighReceipt.query.join(SlakeBatch, WeighReceipt.batch_id == SlakeBatch.id)
        .join(Pond, SlakeBatch.pond_id == Pond.id)
        .join(Plant, Pond.plant_id == Plant.id)
    )

    if scope == "active":
        query = query.filter(WeighReceipt.voided_at.is_(None))
    elif scope == "void":
        query = query.filter(WeighReceipt.voided_at.isnot(None))
    if plant_id:
        query = query.filter(Pond.plant_id == plant_id)
    if pond_id:
        query = query.filter(Pond.id == pond_id)

    receipts = query.order_by(WeighReceipt.weighed_at.desc(), WeighReceipt.id.desc()).all()

    plants = Plant.query.order_by(Plant.name).all()
    ponds = Pond.query.order_by(Pond.code).all()
    return render_template(
        "receipts/list.html",
        receipts=receipts,
        plants=plants,
        ponds=ponds,
        f_plant_id=plant_id,
        f_pond_id=pond_id,
        f_scope=scope,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create_receipt():
    ponds = Pond.query.order_by(Pond.code).all()
    if request.method == "POST":
        pond_id = request.form.get("pond_id", type=int)
        net_raw = (request.form.get("net_weight_t") or "").strip()
        weighed_raw = (request.form.get("weighed_at") or "").strip()
        weigher = (request.form.get("weigher") or "").strip()

        pond = db.session.get(Pond, pond_id) if pond_id else None
        if pond is None:
            flash("请选择熟化池", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        try:
            net_weight = float(net_raw)
        except ValueError:
            flash("净重吨必须是数字", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)
        if net_weight <= 0:
            flash("净重须大于 0", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        if not weigher:
            flash("请填写司磅人", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        try:
            weighed_at = _parse_weighed_at(weighed_raw) if weighed_raw else datetime.now(timezone.utc)
        except ValueError:
            flash("过磅时刻格式无效", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        if pond.status != Pond.STATUS_DRAWN:
            flash("仅本轮已出灰的池可以交称重回执", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        batch = latest_batch_for_pond(pond)
        if batch is None:
            flash("该池尚无熟化批次，无法交回执", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        # 应用层预检，给出友好提示
        if active_receipt_for_round(pond) is not None:
            flash("该池本轮已有合格且未作废的回执，不得重复交据", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        try:
            inserted = WeighReceipt.try_insert_active(
                batch_id=batch.id,
                net_weight_t=net_weight,
                weighed_at=weighed_at,
                weigher=weigher,
                created_by_id=current_user.id,
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("该池本轮已有未作废回执（并发抢交仅落一张）", "error")
            return render_template("receipts/form.html", ponds=ponds, receipt=None)

        if not inserted:
            # 两名司磅并发抢交：唯一索引挡下，请求本身仍成功返回，专页可正常打开
            flash("该池本轮已有未作废回执，本张未入库", "error")
        else:
            flash(f"{pond.code} 称重回执已交，可放行下一轮", "ok")
        return redirect(url_for("receipts.list_receipts", pond_id=pond.id))

    return render_template("receipts/form.html", ponds=ponds, receipt=None)


@bp.route("/<int:receipt_id>/void", methods=["POST"])
@login_required
def void_receipt(receipt_id: int):
    if current_user.role != "admin":
        flash("只有管理员可以作废称重回执", "error")
        return redirect(url_for("receipts.list_receipts"))
    receipt = db.session.get(WeighReceipt, receipt_id)
    if receipt is None:
        flash("回执不存在或已被删除", "error")
        return redirect(url_for("receipts.list_receipts"))
    pond_id = receipt.batch.pond_id if receipt.batch else None
    if receipt.voided_at is not None:
        flash("该回执已作废，无需重复操作", "error")
    else:
        receipt.voided_at = datetime.now(timezone.utc)
        receipt.voided_by_id = current_user.id
        receipt.void_reason = (request.form.get("void_reason") or "").strip()
        db.session.commit()
        flash("回执已作废，不得再凭它放行", "ok")
    return redirect(url_for("receipts.list_receipts", pond_id=pond_id))
