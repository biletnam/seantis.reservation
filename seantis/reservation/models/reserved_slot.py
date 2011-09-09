from sqlalchemy import types
from sqlalchemy.schema import Column
from sqlalchemy.schema import ForeignKey
from sqlalchemy.orm import relation
from sqlalchemy.orm import backref

from seantis.reservation import ORMBase
from seantis.reservation.models import customtypes
from seantis.reservation.models.allocation import Allocation

class ReservedSlot(ORMBase):
    """Describes a reserved slot within an allocated timespan."""

    __tablename__ = 'reserved_slots'

    resource = Column(
        customtypes.GUID(),
        primary_key=True,
        nullable=False,
        autoincrement=False
    )

    start = Column(
        types.DateTime(),
        primary_key=True,
        nullable=False,
        autoincrement=False
    )

    end = Column(
        types.DateTime(),
        nullable=False
    )

    allocation_id = Column(
        types.Integer(), 
        ForeignKey(Allocation.id),
        nullable=False
    )

    allocation = relation(Allocation,
        primaryjoin=Allocation.id==allocation_id,
        backref=backref('reserved_slots', lazy='dynamic', cascade='all')
    )

    reservation = Column(
        customtypes.GUID(),
        nullable = False
    )