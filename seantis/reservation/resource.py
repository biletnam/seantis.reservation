import json
from datetime import datetime

from five import grok
from plone.dexterity.content import Container
from plone.uuid.interfaces import IUUID
from plone.memoize import view

from seantis.reservation import exposure
from seantis.reservation import utils
from seantis.reservation.db import Scheduler
from seantis.reservation import _
from seantis.reservation.timeframe import timeframes_by_context
from seantis.reservation.form import AllocationGroupView
from seantis.reservation.interfaces import IResourceBase

class Resource(Container):

    # Do not use @property here as it messes with the acquisition context.
    # Don't know why.. it worked for me in other cases.

    def uuid(self):
        return IUUID(self)

    def scheduler(self):
        uuid = str(self.uuid())
        is_exposed = exposure.for_allocations(self, [uuid])
        return Scheduler(self.uuid(), self.quota, is_exposed)

    def timeframes(self):
        return timeframes_by_context(self)


class View(grok.View):
    permission = 'zope2.View'

    grok.context(IResourceBase)
    grok.require(permission)
    
    template = grok.PageTemplateFile('templates/resource.pt')

    @view.memoize
    def resources(self):
        uids = self.request.get('compare_to', [])
        if not hasattr(uids, '__iter__'):
            uids = [uids]

        resources = [self.context]
        for uid in uids:
            resource = utils.get_resource_by_uuid(self.context, uid)
            resources.append(resource.getObject())

        template = 'seantis-reservation-calendar-%i'
        for ix, resource in enumerate(resources):
            setattr(resource, 'calendar_id', template % ix)

        return resources

    def javascript(self):
        template = """
        <script type="text/javascript">
            if (!this.seantis) this.seantis = {};
            if (!this.seantis.calendars) this.seantis.calendars = [];

            %s
        </script>
        """

        resources = self.resources()
        min_h = min([r.first_hour for r in resources])
        max_h = max([r.last_hour for r in resources])

        calendars = []
        for ix, resource in enumerate(self.resources()):
            calendars.append(self.calendar_options(ix, resource, min_h, max_h))

        return template % '\n'.join(calendars)

    def calendar_options(self, ix, resource, first_hour=None, last_hour=None):
        template = """
        this.seantis.calendars.push({
            id:'#%s',
            options:%s,
            addurl:'%s',
        })        
        """
        baseurl = resource.absolute_url_path()
        addurl = baseurl + '/allocate'
        eventurl = baseurl + '/slots'

        options = {}
        options['events'] = eventurl
        options['minTime'] = first_hour or resource.first_hour
        options['maxTime'] = last_hour or resource.last_hour

        #TODO theoretically, multiple calendars may have different permissions
        #which would be important for the calendar compare view. It's not a very
        #important feature, but it might be needed one day
        is_exposed = exposure.for_calendar(self.context)
        options['selectable'] = is_exposed('selectable')
        options['editable'] = is_exposed('editable')

        return template % (resource.calendar_id, json.dumps(options), addurl)

    @property
    def calendar_count(self):
        return len(self.resources())


class GroupView(grok.View, AllocationGroupView):
    permission = 'zope2.View'

    grok.context(IResourceBase)
    grok.require(permission)
    grok.name('group')

    group = None
    template = grok.PageTemplateFile('templates/group.pt')

    def update(self, **kwargs):
        self.group = self.request.get('name', u'').decode('utf-8')

    def title(self):
        return self.group
        

class CalendarRequest(object):

    @property
    def range(self):
        start = self.request.get('start', None)
        end = self.request.get('end', None)
        
        if not all((start, end)):
            return None, None

        start = datetime.fromtimestamp(float(start))
        end = datetime.fromtimestamp(float(end))

        return start, end

    def render(self, **kwargs):
        start, end = self.range
        if not all((start, end)):
            return json.dumps([])

        events = self.events()
        
        return json.dumps(events)

    def events(self):
        raise NotImplementedError

class Slots(grok.View, CalendarRequest):
    permission = 'zope2.View'

    grok.context(IResourceBase)
    grok.require(permission)
    grok.name('slots')

    def render(self):
        return CalendarRequest.render(self)

    @property
    def resource(self):
        return self.context

    @property
    def scheduler(self):
        return self.context.scheduler()

    def urls(self, allocation):
        """Returns the options for the js contextmenu for the given allocation
        as well as other links associated with the event.

        """
        
        items = utils.EventUrls(self.context, self.request, exposure)
        has_reservations = self.scheduler.has_reservation_in_range(
                allocation.start, allocation.end
            )

        start = utils.timestamp(allocation.display_start)
        end = utils.timestamp(allocation.display_end)

        # default link to be used when clicking the event
        items.default_url('reserve', dict(start=start, end=end))
        items.move_url('edit-allocation', dict(id=allocation.id))

        # Reservation
        res_add = lambda n, v, p, t: items.menu_add(_(u'Reservations'), n, v, p, t)
        if allocation.is_separate:

            res_add(_(u'Reserve'), 
                'reserve', dict(id=allocation.id, start=start, end=end), 'overlay')

            if has_reservations:
                res_add(_(u'Manage'), 
                    'reservations', dict(id=allocation.id), 'inpage')

        else:

            res_add(_(u'Reserve'),
                'reserve-group', dict(group=allocation.group), 'overlay')

            if has_reservations:
                res_add(_(u'Manage'), 
                    'reservations', dict(group=allocation.group), 'inpage')

        # menu entries for single items
        entry_add = lambda n, v, p, t: items.menu_add(_('Entry'), n, v, p, t)

        entry_add(_(u'Edit'), 
            'edit-allocation', dict(id=allocation.id), 'overlay')

        entry_add(_(u'Remove'), 
            'remove-allocation', dict(id=allocation.id), 'overlay')

        if not allocation.in_group:
            return items

        # menu entries for group items
        group_add = lambda n, v, p, t: items.menu_add(_('Recurrences'), n, v, p, t)
        
        group_add(_(u'List'), 
            'group', dict(name=allocation.group), 'overlay')

        group_add(_(u'Remove'), 
            'remove-allocation', dict(group=allocation.group), 'overlay')

        return items

    def events(self):
        resource = self.context
        scheduler = resource.scheduler()

        is_exposed = exposure.for_allocations(resource, [resource])

        # get an event for each exposed allocation
        events = []
        for alloc in scheduler.allocations_in_range(*self.range):

            if not is_exposed(alloc):
                continue

            start, end = alloc.display_start, alloc.display_end
            
            # get the urls
            urls = self.urls(alloc)
          
            # calculate the availability for title and class
            title, klass = utils.event_availability(
                    resource, self.request, scheduler, alloc
                )

            if not alloc.partly_available:
                # TODO get rid of this workaround
                # (it's about showing a used partition when the master is
                # available but not the mirrors)
                reserved = klass != utils.event_class(100)
                partitions = ((100.0, reserved),)
            else:
                partitions = alloc.availability_partitions()

            events.append(dict(
                title=title, 
                start=start.isoformat(),
                end=end.isoformat(),
                className=klass,
                url=urls.default,
                menu=urls.menu,
                menuorder=urls.order,
                allocation = alloc.id,
                partitions = partitions,
                group = alloc.group,
                allDay=False,
                moveurl=urls.move
            ))
        
        return events