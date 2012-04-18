from textwrap import dedent

from email.MIMEText import MIMEText
from email.Header import Header
from email.Utils import parseaddr, formataddr

from zope.schema import TextLine
from zope.interface import Invalid

from Products.CMFDefault.utils import checkEmailAddress
from Products.CMFDefault.exceptions import EmailAddressInvalid

from seantis.reservation.form import ReservationDataView
from seantis.reservation import utils
from seantis.reservation import _

# TODO -> Move this to a separate module as it is also used in seantis.dir.base
def validate_email(value):
    try:
        if value:
            checkEmailAddress(value)
    except EmailAddressInvalid:
        raise Invalid(_(u'Invalid email address'))
    return True

class EmailField(TextLine):

    def __init__(self, *args, **kwargs):
        super(TextLine, self).__init__(*args, **kwargs)

    def _validate(self, value):
        super(TextLine, self)._validate(value)
        validate_email(value)

# referenced by configuration.zcml to register the Email fields
from plone.schemaeditor.fields import FieldFactory
EmailFieldFactory = FieldFactory(EmailField, _(u'Email'))

from plone.supermodel.exportimport import BaseHandler
EmailFieldHandler = BaseHandler(EmailField)

def send_reservation_made_for_managers(reservation, with_approval):
    resource = utils.get_resource_by_uuid(reservation.resource)

    subject = _(u'New reservation for %(resource)s')
    body = dedent("""\
        A new reservation was made for %(resource)s:

        Dates:
        %(dates)s

        Email: 
        %(reservation_mail)s
        
        Formdata: 
        %(data)s

        Sincerely yours,

        The seantis.reservation Mailbot
    """)

    mail = ReservationMail(resource, reservation, 
        sender='bot@example.com',
        recipient=reservation.email,
        subject=subject,
        body=body
        )

    send_mail(resource, mail)

def send_mail(context, mail):
    try:
        context.MailHost.send(mail.as_string(), immediate=True)
    except Exception:
        pass # TODO add logging

class ReservationMail(object, ReservationDataView):

    sender=u'', 
    recipient=u'',
    subject=u'',
    charset=u'utf-8'
    body=u''

    def __init__(self, resource, reservation, **kwargs):
        for k,v in kwargs.items():
            if hasattr(self, k): setattr(self, k, v)

        # get information for the body/subject string

        p = dict()
        is_needed = lambda key: key in self.subject or key in self.body

        # title of the resource
        if is_needed('resource'):
            p['resource'] = utils.get_resource_title(resource)

        # reservation email
        if is_needed('reservation_mail'):
            p['reservation_mail'] = self.recipient

        # a list of dates
        if is_needed('dates'):
            lines = []
            dates = sorted(reservation.timespans(), key=lambda i: i[0])
            for start, end in dates:
                line = start.strftime('%d.%m.%Y %H:%M')
                line += ' - '
                line += end.strftime('%H:%M')

                lines.append(line)

            p['dates'] = '\n'.join(lines)

        # tabbed reservation data
        if is_needed('data'):
            data = reservation.data
            lines = []
            for key in self.sorted_info_keys(data):
                interface = data[key]

                lines.append(interface['desc'])
                for value in self.sorted_values(interface['values']):
                    lines.append('\t' + self.display_info(value['value']))


            p['data'] = '\n'.join(lines)

        # approval link
        if is_needed('approval_link'):
            pass

        # denial links
        if is_needed('denial_link'):
            pass

        self.parameters = p

    def mail_text(self):
        subject = self.subject % self.parameters
        body = self.body % self.parameters
        mail = create_email(self.sender, self.recipient, subject, body)
        return mail.as_string()

def create_email(sender, recipient, subject, body):
    """Create an email message.

    All arguments should be Unicode strings (plain ASCII works as well).

    Only the real name part of sender and recipient addresses may contain
    non-ASCII characters.

    The charset of the email will be the UTF-8.
    """

    header_charset = 'UTF-8'
    body_charset = 'UTF-8'

    body.encode(body_charset)

    # Split real name (which is optional) and email address parts
    sender_name, sender_addr = parseaddr(sender)
    recipient_name, recipient_addr = parseaddr(recipient)

    # We must always pass Unicode strings to Header, otherwise it will
    # use RFC 2047 encoding even on plain ASCII strings.
    sender_name = str(Header(unicode(sender_name), header_charset))
    recipient_name = str(Header(unicode(recipient_name), header_charset))

    # Make sure email addresses do not contain non-ASCII characters
    sender_addr = sender_addr.encode('ascii')
    recipient_addr = recipient_addr.encode('ascii')

    # Create the message ('plain' stands for Content-Type: text/plain)
    msg = MIMEText(body.encode(body_charset), 'plain', body_charset)
    msg['From'] = formataddr((sender_name, sender_addr))
    msg['To'] = formataddr((recipient_name, recipient_addr))
    msg['Subject'] = Header(unicode(subject), header_charset)

    return msg