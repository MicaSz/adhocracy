import re
import formencode
from formencode import validators, foreach
from pylons.i18n.translation import *


FORBIDDEN_NAMES = ["www", "static", "mail", "edit", "create", "settings", "join", "leave", 
                   "control", "test", "support", "page", "issue", "proposal", "wiki", 
                   "blog", "issues", "proposals", "admin", "dl", "downloads", "stats",
                   "adhocracy", "user", "openid", "auth", "watch", "poll", "delegation",
                   "event", "comment", "root", "search", "tag", "svn", "trac", "lists", 
                   "list", "new", "update", "variant", "provision"]


VALIDUSER = re.compile("^[a-zA-Z0-9_\-]{3,255}$")


class UniqueUsername(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import meta, User
        if not value or not isinstance(value, basestring):
            raise formencode.Invalid(
                _('No username is given'),
                value, state)
        if len(value) < 3:
            raise formencode.Invalid(
                _('Username is too short'),
                value, state)
        if not VALIDUSER.match(value) or value in FORBIDDEN_NAMES:
            raise formencode.Invalid(
                _('The username is invalid'),
                value, state)
        if meta.Session.query(User.user_name).filter(User.user_name==value).all():
            raise formencode.Invalid(
                _('That username already exists'),
                value, state)
        return value

class UniqueEmail(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import meta, User
        email = value.lower()
        if meta.Session.query(User.email).filter(User.email == email).all():
            raise formencode.Invalid(
                _('That email is already registered'),
                value, state)
        return value

class UniqueInstanceKey(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Instance
        if not value:
            raise formencode.Invalid(
                _('No instance key is given'),
                value, state)
        if not Instance.INSTANCE_KEY.match(value) or value in FORBIDDEN_NAMES:
            raise formencode.Invalid(
                _('The instance key is invalid'),
                value, state)
        if Instance.find(value):
            raise formencode.Invalid(
                _('An instance with that key already exists'),
                value, state)
        return value

class ValidDelegateable(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Delegateable
        delegateable = Delegateable.find(value)
        if not delegateable: 
           raise formencode.Invalid(
                _("No entity with ID '%s' exists") % value,
                 value, state)
        return delegateable

class ValidProposal(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Proposal
        proposal = Proposal.find(value)
        if not proposal: 
           raise formencode.Invalid(
                _("No proposal with ID '%s' exists") % value,
                 value, state)
        return proposal

class ValidGroup(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Group
        group = Group.by_code(value)
        if not group: 
           raise formencode.Invalid(
                _("No group with ID '%s' exists") % value,
                 value, state)
        return group
    
class ValidRevision(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Revision
        revision = Revision.find(value)
        if not revision: 
           raise formencode.Invalid(
                _("No revision with ID '%s' exists") % value,
                 value, state)
        return revision
        
class ValidComment(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Comment
        comment = Comment.find(value)
        if not comment: 
           raise formencode.Invalid(
                _("No comment with ID '%s' exists") % value,
                 value, state)
        return comment

class ValidWatch(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Watch
        watch = Watch.by_id(value)
        if not watch: 
           raise formencode.Invalid(
                _("No watchlist entry with ID '%s' exists") % value,
                 value, state)
        return watch
        
class ValidRef(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import refs
        try:
            entity = refs.from_url(value)
            if not entity: 
                raise TypeError()
            return entity
        except: 
            raise formencode.Invalid(_("Invalid reference"), value, state)
        
class ExistingUserName(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import User
        user = User.find(value)
        if not user: 
           raise formencode.Invalid(
                _("No user with the user name '%s' exists") % value,
                value, state)
        return user

class ValidTagging(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Tagging
        tagging = Tagging.find(value)
        if not tagging: 
           raise formencode.Invalid(
                _("No tagging with ID '%s' exists") % value,
                 value, state)
        return tagging
        
class ValidText(formencode.FancyValidator):
    def _to_python(self, value, state):
        from adhocracy.model import Text
        text = Text.find(value)
        if not text: 
            raise formencode.Invalid(
                _("No text with ID '%s' exists") % value,
                         value, state)
        return text
