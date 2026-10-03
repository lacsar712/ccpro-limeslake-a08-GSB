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


def latest_valid_receipt_for_pond(pond: Pond) -> WeighReceipt | None:
    """本轮（最近一批次）最新一张合格且未作废的称重回执；没有则 None。"""
    batch = latest_batch_for_pond(pond)
    if batch is None:
        return None
    active = [r for r in batch.receipts if r.voided_at is None]
    if not active:
        return None
    return max(active, key=lambda r: r.weighed_at)


def can_mark_pond_drawn(pond: Pond) -> tuple[bool, str]:
    """
    熟化池转为「已出灰」(drawn) 的前提：
    最近一条熟化批次的峰值温度已记录，且 >= 60℃。
    称重不参与出灰判断。
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
        # 已出灰 -> 非已出灰（拨回注水/熟化）前，本轮必须有合格未作废回执。
        if latest_valid_receipt_for_pond(pond) is None:
            raise RuleError(
                "本轮出灰尚未交合格称重回执，不得拨回注水或熟化；"
                "请先在称重回执专页交单"
            )


def assert_can_start_batch(pond: Pond) -> None:
    """在已出灰池上开新班前，必须先有本轮合格未作废回执。"""
    if pond.status == Pond.STATUS_DRAWN:
        if latest_valid_receipt_for_pond(pond) is None:
            raise RuleError(
                f"{pond.code} 本轮出灰尚未交合格称重回执，不得开下一班注水"
            )
