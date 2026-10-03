"""石灰熟化池业务规则。"""

from __future__ import annotations

from app.models import Pond, SlakeBatch, WeighReceipt

MIN_PEAK_TEMP_FOR_DRAWN = 60.0


class RuleError(ValueError):
    """业务规则校验失败。"""


def latest_batch_for_pond(pond: Pond) -> SlakeBatch | None:
    if not pond.batches:
        return None
    return max(pond.batches, key=lambda b: b.started_at)


def active_receipt_for_round(pond: Pond) -> WeighReceipt | None:
    """本轮（最近一个熟化批次）最新一张合格、未作废的称重回执。"""
    latest = latest_batch_for_pond(pond)
    if latest is None:
        return None
    return (
        WeighReceipt.query.filter(
            WeighReceipt.batch_id == latest.id,
            WeighReceipt.voided_at.is_(None),
            WeighReceipt.net_weight_t > 0,
        )
        .order_by(WeighReceipt.weighed_at.desc(), WeighReceipt.id.desc())
        .first()
    )


def can_mark_pond_drawn(pond: Pond) -> tuple[bool, str]:
    """
    熟化池转为「已出灰」(drawn) 的前提：
    最近一条熟化批次的峰值温度已记录，且 >= 60℃。
    称重回执不参与出灰判断。
    """
    latest = latest_batch_for_pond(pond)
    if latest is None:
        return False, "该池尚无熟化批次，不能标记为已出灰"
    if latest.peak_temp_c is None:
        return False, "最近批次尚未记录峰值温度，不能标记为已出灰"
    if latest.peak_temp_c < MIN_PEAK_TEMP_FOR_DRAWN:
        return (
            False,
            f"最近批次峰值温度 {latest.peak_temp_c}℃ 低于 {MIN_PEAK_TEMP_FOR_DRAWN:.0f}℃，不能标记为已出灰",
        )
    return True, ""


def assert_can_set_pond_status(pond: Pond, new_status: str) -> None:
    if new_status not in Pond.STATUS_CHOICES:
        raise RuleError(f"无效状态：{new_status}")
    if new_status == Pond.STATUS_DRAWN:
        ok, msg = can_mark_pond_drawn(pond)
        if not ok:
            raise RuleError(msg)
    elif pond.status == Pond.STATUS_DRAWN:
        # 已出灰 → 注水/熟化（拨回）：必须先交合格称重回执
        if active_receipt_for_round(pond) is None:
            target = {
                Pond.STATUS_FILLING: "注水中",
                Pond.STATUS_SLAKING: "熟化中",
            }.get(new_status, new_status)
            raise RuleError(
                f"本轮出灰后尚无合格且未作废的称重回执，不得把池从已出灰拨回{target}"
            )


def assert_can_start_batch(pond: Pond) -> WeighReceipt | None:
    """在已出灰池上开新班（下一轮注水）前，必须持有本轮回执。"""
    if pond.status == Pond.STATUS_DRAWN:
        receipt = active_receipt_for_round(pond)
        if receipt is None:
            raise RuleError(
                "本轮出灰后尚无合格且未作废的称重回执，不得开下一班注水"
            )
        return receipt
    return None
