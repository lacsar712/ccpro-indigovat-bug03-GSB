from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)


class Workshop(Base):
    __tablename__ = "workshops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, default="")

    vats: Mapped[list["Vat"]] = relationship(back_populates="workshop")


class Vat(Base):
    __tablename__ = "vats"
    __table_args__ = (
        UniqueConstraint("workshop_id", "code", name="uniq_vat_code_per_workshop"),
    )

    STATUS_IDLE = "idle"
    STATUS_REDUCING = "reducing"
    STATUS_READY = "ready"

    id: Mapped[int] = mapped_column(primary_key=True)
    workshop_id: Mapped[int] = mapped_column(ForeignKey("workshops.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(40))
    dyeType: Mapped[str] = mapped_column(String(80))
    volumeL: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_IDLE)

    workshop: Mapped["Workshop"] = relationship(back_populates="vats")
    lots: Mapped[list["DipLot"]] = relationship(back_populates="vat")

    # 全应用唯一的缸内批次排序键：先按浸染时间，再按主键兜底并列。
    # 缸位条折线、展开区近笔、状态校验用的「最新批次」都必须走它。
    @staticmethod
    def lot_sort_key(lot: "DipLot"):
        return (lot.dippedAt, lot.id)

    def ordered_lots(self) -> list["DipLot"]:
        """旧 → 新，唯一顺序。"""
        return sorted(self.lots, key=self.lot_sort_key)

    def latest_lot(self) -> Optional["DipLot"]:
        if not self.lots:
            return None
        return max(self.lots, key=self.lot_sort_key)


class DipLot(Base):
    __tablename__ = "dip_lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    vat_id: Mapped[int] = mapped_column(ForeignKey("vats.id", ondelete="CASCADE"))
    dippedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clothMeters: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    redoxMv: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)

    vat: Mapped["Vat"] = relationship(back_populates="lots")
