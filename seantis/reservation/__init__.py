from sqlalchemy.ext import declarative
ORMBase = declarative.declarative_base()

from zope.i18nmessageid import MessageFactory 
_ = MessageFactory('seantis.reservation')

from scheduler import Scheduler