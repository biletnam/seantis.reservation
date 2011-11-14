from uuid import UUID
from uuid import uuid4 as new_uuid
from uuid import uuid5 as new_uuid_mirror
from datetime import datetime, MINYEAR, MAXYEAR
from itertools import groupby

from sqlalchemy.sql import and_, or_
from sqlalchemy.orm import joinedload

from seantis.reservation.models import Allocation
from seantis.reservation.models import ReservedSlot
from seantis.reservation.error import OverlappingAllocationError
from seantis.reservation.error import AffectedReservationError
from seantis.reservation.error import AlreadyReservedError
from seantis.reservation.error import NotReservableError
from seantis.reservation.error import ReservationTooLong
from seantis.reservation.session import serialized
from seantis.reservation.raster import rasterize_span
from seantis.reservation import utils
from seantis.reservation import Session
    
def all_allocations_in_range(start, end):
    # Query version of utils.overlaps
    return Session.query(Allocation).filter(
        or_(
            and_(
                Allocation._start <= start,
                start <= Allocation._end
            ),
            and_(
                start <= Allocation._start,
                Allocation._start <= end
            )
        )
    )

def availability_by_allocations(allocations):
    """Takes any iterator with alloctions and calculates the availability. Counts
    missing mirrors as 100% free and returns a value between 0-100 in any case.
    For single allocations check the allocation.availability property.

    """
    total, expected_count, count = 0, 0, 0
    for allocation in allocations:
        total += allocation.availability
        count += 1

        # Sum up the expected number of allocations. Missing allocations
        # indicate mirrors that have not yet been physically created.
        if allocation.is_master:
            expected_count += allocation.quota

    if not expected_count:
        return 0
    
    missing = expected_count - count
    total += missing * 100

    return total / expected_count

def availability_by_range(start, end, resources, is_exposed):
    """Returns the availability for the given resources in the given range.
    The callback *is_exposed* is used to check if the allocation is visible
    to the current user. This should usually be the exposure.for_allocations
    return value.

    """
    
    query = all_allocations_in_range(start, end)
    query = query.filter(Allocation.mirror_of.in_(resources))
    query = query.options(joinedload(Allocation.reserved_slots))

    allocations = (a for a in query if is_exposed(a))
    return availability_by_allocations(allocations)

def availability_by_day(start, end, resources, is_exposed):
    """Availability by range with a twist. Instead of returning a grand total,
    a dictionary is returned with each day in the range as key and a tuple of
    availability and the resources counted for that day.

    """
    query = all_allocations_in_range(start, end)
    query = query.filter(Allocation.mirror_of.in_(resources))
    query = query.options(joinedload(Allocation.reserved_slots))
    query = query.order_by(Allocation._start)

    group = groupby(query, key=lambda a: a._start.date())
    days = {}

    for day, allocations in group:

        exposed = [a for a in allocations if is_exposed(a)]
        if not exposed:
            continue

        members = set([a.mirror_of for a in exposed])
        days[day] = (availability_by_allocations(exposed), members)

    return days

# TODO cache this incrementally
def generate_uuids(uuid, quota):
    mirror = lambda n: new_uuid_mirror(uuid, str(n))
    return [mirror(n) for n in xrange(1, quota)]

class Scheduler(object):
    """Used to manage a resource as well as all connected mirrors. 

    Master -> Mirror relationship
    =============================

    Mirrors are viewed as resources which mirror a master resource. These mirrors
    do not really exist as seantis.reservation.resource types in plone (unlike
    the master). Instead they have their own resource uuids which are calculated
    by creating new uuids from the master's uuid and the number of the mirror.
    (See generate_uuids).

    The reason for this mechanism is to ensure two things:

    - No more mirrors than required are created (if we tried that we would get
      integrity errors as the resource plus the start-time are unique)

    - The reservation slot does not need to carry any information about the
      mirror. It just references a resource uuid

    Since we do not want to calculate these mirror uuids all the time, since it
    is a somewhat expensive calculations and because it is a bit of hassle, we
    store the master uuid in the mirror_of field of each allocation record.

    """

    def __init__(self, resource_uuid, quota=1, is_exposed=None):
        assert(0 <= quota)

        try: 
            self.uuid = UUID(resource_uuid)
        except AttributeError: 
            self.uuid = resource_uuid
        
        self.is_exposed = is_exposed or (lambda allocation: True)
        self.quota = quota

    @serialized
    def allocate(self, dates, group=None, raster=15, quota=None, partly_available=False):
        """Allocates a spot in the calendar.

        An allocation defines a timerange which can be reserved. No reservations
        can exist outside of existing allocations. In fact any reserved slot will
        link to an allocation.

        An allocation may be available as a whole (to reserve all or nothing).
        It may also be partly available which means reservations can be made
        for parts of the allocation. 

        If an allocation is partly available a raster defines the granularity
        with which a reservation can be made (e.g. a raster of 15min will ensure 
        that reservations are at least 15 minutes long and start either at 
        :00, :15, :30 or :45)

        The reason for the raster is mainly to ensure that different reservations
        trying to reserve overlapping times need the same keys in the reserved_slots
        table, ensuring integrity at the database level.

        Allocations may have a quota, which determines how many times an allocation
        may be reserved. Quotas are enabled using a master-mirrors relationship.

        The master is the first allocation to be created. The mirrors copies of
        that allocation. See Scheduler.__doc__

        """
        dates = utils.pairs(dates)

        group = group or unicode(new_uuid())
        quota = quota or self.quota

        # Make sure that this span does not overlap another master
        for start, end in dates:
            start, end = rasterize_span(start, end, raster)
            
            query = all_allocations_in_range(start, end)
            query = query.filter(Allocation.resource == self.uuid)

            existing = query.first()
            if existing:
                raise OverlappingAllocationError(start, end, existing)
        
        # Write the master allocations
        allocations = []
        for start, end in dates:
            allocation = Allocation(raster=raster)
            allocation.start = start
            allocation.end = end
            allocation.group = group
            allocation.resource = self.uuid
            allocation.quota = quota
            allocation.mirror_of = self.uuid
            allocation.partly_available = partly_available
                
            allocations.append(allocation)

        Session.add_all(allocations)

        return group, allocations

    @serialized
    def change_quota(self, master, new_quota):
        """ Changes the quota of a master allocation.

        Fails if the quota is already exhausted.

        When the quota is decreased a reorganization of the mirrors is triggered.
        Reorganizing means eliminating gaps in the chain of mirrors that emerge
        when reservations are removed:

        Initial State:
        1   (master)    Free
        2   (mirror)    Free
        3   (mirror)    Free

        Reservations are made:
        1   (master)    Reserved
        2   (mirror)    Reserved
        3   (mirror)    Reserved

        A reservation is deleted:
        1   (master)    Reserved
        2   (mirror)    Free     <-- !!
        3   (mirror)    Reserved

        Reorganization is performed:
        1   (master)    Reserved
        2   (mirror)    Reserved <-- !!
        3   (mirror)    Free     <-- !!

        The quota is decreased:
        1   (master)    Reserved
        2   (mirror)    Reserved

        In other words, the reserved allocations are moved to the beginning,
        the free allocations moved at the end. This is done to ensure that
        the sequence of generated uuids for the mirrors always represent all
        possible keys.

        Without the reorganization we would see the following after
        decreasing the quota:

        The quota is decreased:
        1   (master)    Reserved
        3   (mirror)    Reserved

        This would make it impossible to calculate the mirror keys. Instead the
        existing keys would have to queried from the database.
        
        """

        assert new_quota > 0, "Quota must be greater than 0"

        if new_quota == master.quota:
            return

        if new_quota > master.quota:
            master.quota = new_quota
            return

        # Make sure that the quota can be decreased
        mirrors = self.allocation_mirrors_by_master(master)
        allocations = [master] + mirrors

        free_allocations = [a for a in allocations if a.is_available]

        required = master.quota - new_quota
        if len(free_allocations) < required:
            raise AffectedReservationError(None)
        
        # get a map pointing from the existing uuid to the newly assigned uuid
        reordered = self.reordered_keylist(allocations, new_quota)

        # unused keys are the ones not present in the newly assignd uuid list
        unused = set(reordered.keys()) - set(reordered.values()) - set((None,))
        
        # get a map for resource_uuid -> allocation.id
        ids = dict(((a.resource, a.id) for a in allocations))

        for allocation in allocations:

            # change the quota for all allocations
            allocation.quota = new_quota

            # the value is None if the allocation is not mapped to a new uuid
            new_resource = reordered[allocation.resource]
            if not new_resource:
                continue
            
            # move all slots to the mapped allocation id
            new_id = ids[new_resource]

            for slot in allocation.reserved_slots:
                # build a query here as the manipulation of mapped objects in
                # combination with the delete query below seems a bit
                # unpredictable given the cascading of changes

                query = Session.query(ReservedSlot)
                query = query.filter(and_(
                        ReservedSlot.resource == slot.resource,
                        ReservedSlot.allocation_id == slot.allocation_id,
                        ReservedSlot.start == slot.start
                    ))
                query.update(
                        {
                            ReservedSlot.resource: new_resource, 
                            ReservedSlot.allocation_id: new_id
                        }
                    )
                

        # get rid of the unused allocations (always preserving the master)
        if unused:
            query = Session.query(Allocation)
            query = query.filter(Allocation.resource.in_(unused))
            query = query.filter(Allocation.id != master.id)
            query = query.filter(Allocation._start == master._start)
            query.delete('fetch')
        
    
    def reordered_keylist(self, allocations, new_quota):
        """ Creates the map for the keylist reorganzation. 

        Each key of the returned dictionary is a resource uuid pointing to the 
        resource uuid it should be moved to. If the allocation should not be
        moved they key-value is None.

        """
        masters = [a for a in allocations if a.is_master]
        assert(len(masters) == 1)

        master = masters[0]
        allocations = dict(((a.resource, a) for a in allocations))
        
        # generate the keylist (the allocation resources may be unordered)
        keylist = [master.resource]
        keylist.extend(generate_uuids(master.resource, master.quota))
        
        # prefill the map
        reordered = dict(((k, None) for k in keylist))

        # each free allocation increases the offset by which the next key
        # for a non-free allocation is acquired
        offset = 0
        for ix, key in enumerate(keylist):
            if allocations[key].is_available():
                offset += 1
            else:
                reordered[key] = keylist[ix - offset]

        return reordered


    def allocation_by_id(self, id):
        query = Session.query(Allocation)
        query = query.filter(Allocation.resource == self.uuid)
        query = query.filter(Allocation.id == id)
        return query.one()

    def allocations_by_group(self, group):
        query = Session.query(Allocation)
        query = query.filter(Allocation.group == group)
        query = query.filter(Allocation.resource == self.uuid)
        return query

    def allocations_in_range(self, start, end):
        query = all_allocations_in_range(start, end)
        query = query.filter(Allocation.resource == self.uuid)
        return query

    def allocations_by_reservation(self, reservation):
        query = Session.query(Allocation).join(ReservedSlot)
        query = query.filter(ReservedSlot.reservation == reservation)
        return query

    def allocation_by_date(self, start, end):
        query = self.allocations_in_range(start, end)
        return query.one()

    def allocation_mirrors_by_master(self, master):
        if master.quota == 1: return []

        query = Session.query(Allocation)
        query = query.filter(Allocation._start == master._start)
        query = query.filter(Allocation.id != master.id)
        
        existing = query.all()
        existing = dict([(e.resource, e) for e in existing])

        imaginary = master.quota - len(existing)
        
        mirrors = []
        for uuid in generate_uuids(master.resource, master.quota):
            if uuid in existing:
                mirrors.append(existing[uuid])
            elif imaginary:
                allocation = master.copy()
                allocation.resource = uuid
                mirrors.append(allocation)

                imaginary -= 1

        return mirrors

    def reservable(self, allocation):
        return self.is_exposed(allocation)

    def render_allocation(self, allocation):
        return self.is_exposed(allocation)

    def availability(self, start=None, end=None):
        """Goes through all allocations and sums up the availabilty."""

        if not (start and end):
            start = datetime(MINYEAR, 1, 1)
            end = datetime(MAXYEAR, 12, 31)

        return availability_by_range(start, end, [self.uuid], self.is_exposed)

    @serialized
    def move_allocation(self, master_id, new_start=None, new_end=None, 
                            group=None, new_quota=None):

        assert master_id
        assert any([new_start and new_end, group, new_quota])

        # Find allocation
        master = self.allocation_by_id(master_id)
        mirrors = self.allocation_mirrors_by_master(master)
        changing = [master] + mirrors
        ids = [c.id for c in changing]

        assert(group or master.group)

        # Simulate the new allocation
        new = Allocation(start=new_start, end=new_end, raster=master.raster)

        # Ensure that the new span does not overlap an existing one
        existing_allocations = self.allocations_in_range(new.start, new.end)

        for existing in existing_allocations:
            if existing.id not in ids:
                raise OverlappingAllocationError(new.start, new.end, existing)

        for change in changing:
            if change.partly_available:
                for reservation in change.reserved_slots:
                    if not new.contains(reservation.start, reservation.end):
                        raise AffectedReservationError(reservation)
            else:
                if change.start != new.start or change.end != new.end:
                    if len(change.reserved_slots):
                        raise AffectedReservationError(change.reserved_slots[0])

        if new_quota is not None:
            self.change_quota(master, new_quota)

        for change in changing:
            change.start = new.start
            change.end = new.end
            change.group = group or master.group

    @serialized
    def remove_allocation(self, id=None, group=None):
        if id:
            master = self.allocation_by_id(id)
            allocations = [master]
            allocations.extend(self.allocation_mirrors_by_master(master))
        elif group:
            query = Session.query(Allocation)
            query = query.filter(Allocation.group == group)
            query = query.filter(Allocation.mirror_of == self.uuid)
            allocations = query.all()
        else:
            raise NotImplementedError
        
        for allocation in allocations:
            if len(allocation.reserved_slots) > 0:
                raise AffectedReservationError(allocation.reserved_slots[0])

        for allocation in allocations:
            if not allocation.is_transient:
                Session.delete(allocation)

    @serialized
    def reserve(self, dates):
        """ Tries to reserve a number of dates. If dates are found which are
        already reserved, an AlreadyReservedError is thrown. If a reservation
        is made between the availability check and the reservation an integrity
        error will surface once the session is flushed.

        """
        dates = utils.pairs(dates)
        reservation = new_uuid()
        slots_to_reserve = []

        for start, end in dates:

            if abs((end - start).days) > 0:
                raise ReservationTooLong

            assert start < end
            assert (end - start).seconds >= 5 * 60

            for allocation in self.reservation_targets(start, end):
                
                if not self.reservable(allocation):
                    continue

                for slot_start, slot_end in allocation.all_slots(start, end):
                    slot = ReservedSlot()
                    slot.start = slot_start
                    slot.end = slot_end
                    slot.resource = allocation.resource
                    slot.reservation = reservation

                    allocation.reserved_slots.append(slot)
                    slots_to_reserve.append(slot)

                if allocation.is_transient:
                    Session.add(allocation)

        if not slots_to_reserve:
            raise NotReservableError

        return reservation, slots_to_reserve

    def find_spot(self, master_allocation, start, end):
        """ Returns the first free allocation spot amongst the master and the
        mirrors. Honors the quota set on the master and will only try the master
        if the quota is set to 1.

        If no spot can be found, None is returned.

        """
        master = master_allocation
        if master.is_available(start, end):
            return master

        if master.quota == 1:
            return None
        
        mirrors = self.allocation_mirrors_by_master(master)
        tries = master.quota - 1

        for mirror in mirrors:
            if mirror.is_available(start, end):
                return mirror

            if tries >= 1:
                tries -= 1
            else:
                return None

    def reservation_targets(self, start, end):
        """ Returns a list of allocations that are free within start and end.
        These allocations may come from the master or any of the mirrors. 

        """
        targets = []

        query = all_allocations_in_range(start, end)
        query = query.filter(Allocation.resource == self.uuid)

        for master_allocation in query:
            if not master_allocation.overlaps(start, end):
                continue # may happen because start and end are not rasterized

            found = self.find_spot(master_allocation, start, end)
            
            if not found:
                raise AlreadyReservedError

            targets.append(found)

        return targets

    def managed_reserved_slots(self):
        """Returns the reserved slots which are managed by this scheduler."""
        query = Session.query(ReservedSlot)
        query = query.join(Allocation)
        query = query.filter(Allocation.mirror_of == self.uuid)

        return query

    def reserved_slots(self, reservation):
        """Returns all reserved slots of the given reservation."""
        query = self.managed_reserved_slots()
        query = query.filter(ReservedSlot.reservation == reservation)

        return query

    def reservations_for_range(self, start, end):
        query = self.managed_reserved_slots()
        query = query.filter(start <= ReservedSlot.start)
        query = query.filter(ReservedSlot.end <= end)

        return query

    def reservations_for_group(self, group):
        query = self.managed_reserved_slots()
        query = query.filter(Allocation.group == group)

        return query

    def reservations_for_allocation(self, allocation_id):
        query = self.managed_reserved_slots()
        query = query.filter(ReservedSlot.allocation_id == allocation_id)

        return query

    @serialized
    def remove_reservation(self, reservation, id=None):
        assert reservation

        query = self.reserved_slots(reservation)

        if id: 
            query = query.filter(ReservedSlot.allocation_id == id)
        
        for slot in query:
            Session.delete(slot)