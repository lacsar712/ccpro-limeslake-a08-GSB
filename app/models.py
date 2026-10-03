from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import insert as pg_insert
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="worker")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Plant(db.Model):
    __tablename__ = "plants"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(200), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    ponds = db.relationship("Pond", back_populates="plant", cascade="all, delete-orphan")


class Pond(db.Model):
    __tablename__ = "ponds"
    __table_args__ = (
        db.UniqueConstraint("plant_id", "code", name="uq_pond_code_per_plant"),
    )

    STATUS_FILLING = "filling"
    STATUS_SLAKING = "slaking"
    STATUS_DRAWN = "drawn"
    STATUS_CHOICES = (STATUS_FILLING, STATUS_SLAKING, STATUS_DRAWN)

    id = db.Column(db.Integer, primary_key=True)
    plant_id = db.Column(db.Integer, db.ForeignKey("plants.id"), nullable=False)
    code = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_FILLING)
    capacity_m3 = db.Column(db.Float, nullable=False, default=0.0)
    notes = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    plant = db.relationship("Plant", back_populates="ponds")
    batches = db.relationship(
        "SlakeBatch",
        back_populates="pond",
        cascade="all, delete-orphan",
    )


class SlakeBatch(db.Model):
    __tablename__ = "slake_batches"

    id = db.Column(db.Integer, primary_key=True)
    pond_id = db.Column(db.Integer, db.ForeignKey("ponds.id"), nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    target_temp_c = db.Column(db.Float, nullable=False, default=80.0)
    peak_temp_c = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=False, default="")

    pond = db.relationship("Pond", back_populates="batches")
    receipts = db.relationship(
        "WeighReceipt",
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class WeighReceipt(db.Model):
    """出灰称重回执：一轮（一个熟化批次）未作废回执最多一张。"""

    __tablename__ = "weigh_receipts"
    __table_args__ = (
        # 数据库级兜底：两名司磅并发抢交同一轮时，只允许一张未作废回执落库
        db.Index(
            "uq_receipt_one_active_per_batch",
            "batch_id",
            unique=True,
            postgresql_where=db.text("voided_at IS NULL"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("slake_batches.id"), nullable=False)
    net_weight_t = db.Column(db.Float, nullable=False)
    weighed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    weigher = db.Column(db.String(64), nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    voided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    void_reason = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    batch = db.relationship("SlakeBatch", back_populates="receipts", foreign_keys=[batch_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])

    @property
    def is_void(self) -> bool:
        return self.voided_at is not None

    @classmethod
    def try_insert_active(cls, *, batch_id: int, net_weight_t: float,
                          weighed_at: datetime, weigher: str,
                          created_by_id: int) -> bool:
        """
        以 INSERT ... ON CONFLICT DO NOTHING 写入未作废回执；
        同一轮已有未作废回执（含并发抢交）时返回 False。
        """
        stmt = pg_insert(cls).values(
            batch_id=batch_id,
            net_weight_t=net_weight_t,
            weighed_at=weighed_at,
            weigher=weigher,
            created_by_id=created_by_id,
            voided_at=None,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["batch_id"],
            index_where=db.text("voided_at IS NULL"),
        )
        result = db.session.execute(stmt)
        return result.rowcount == 1
