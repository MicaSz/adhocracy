import copy
from inspect import isclass
import math
import logging
import time
import urllib

from formencode import validators
from pylons.i18n import _, lazy_ugettext, lazy_ugettext as L_
from pylons import config, request, tmpl_context as c, url
from pylons.controllers.util import redirect
from webob.multidict import MultiDict

from adhocracy import model
from adhocracy.lib import sorting, tiles
from adhocracy.lib.event.stats import user_activity
from adhocracy.lib.search.query import sunburnt_query, add_wildcard_query
from adhocracy.lib.templating import render_def
from adhocracy.model.refs import ref_attr_value

log = logging.getLogger(__name__)

PAGE_VALIDATOR = validators.Int(min=1, not_empty=True)
SIZE_VALIDATOR = validators.Int(min=1, max=250, not_empty=True)


marker = object()


def sort_key_getter(item):
    entity = item.get('entity', None)
    if entity:
        for attribute in ['title', 'name', 'id']:
            value = getattr(entity, attribute, marker)
            if value is not marker:
                return value
    return item['facet_count'] * -1  # reverse sorting


def visible_pages(selected_page, pages):
    '''
    determinate which page links in a pager are visible
    and where the '...' seperators should be located.
    **Warning**: This code is 1-based!

    *selected_page*
        The selected page (index 1)
    *pages*
        The number of pages (index 1)
    Returns: A *(visible_pages , seperators)* tuple where both
    are lists.
    '''

    ### If we have < 11 pages we show all page links
    ### X X X O X X X X X X X
    if pages <= 11:
        return [range(1, pages + 1), []]

    ### if we have > 11 pages, we select which boxes and
    ### which seperators to show
    # Case: near the start. Show the pages up to 9, a seperator
    # and the last 1
    # X X X X O X X X X ... X
    if selected_page <= 7:
        return [range(1, 9 + 1) + [pages], [10]]
    # Case: near the end. Show the first two pages, the seperator
    # and the last 9
    # X ... X X X X X O X X X
    if (pages - selected_page) <= 7:
        return [[1] + range(pages - 8, pages + 1), [2]]
    # Case: somewhere within the long list
    # X ... X X X O X X X ... X
    return [[1] + range(selected_page - 3, selected_page + 3 + 1) +
            [pages], [2, pages]]


class PagerMixin(object):

    @property
    def offset(self):
        return (self.page - 1) * self.size

    @property
    def pages(self):
        return int(math.ceil(self.total_num_items() / float(self.size)))

    def here(self):
        '''
        backwarts compatibility. Use :meth:`render_pager`.
        '''
        return self.render_pager()

    def page_sizes(self):
        if self.initial_size <= self.total_num_items():
            return []
        page_sizes = []
        # offer page sizes: from the initial size to either half of the
        # total size or 5 x the initial size.
        sizes = range(self.initial_size,
                      min(self.total_num_items() + self.initial_size / 2,
                          (self.initial_size * 5)) + 1,
                      self.initial_size / 2)
        for size in sizes:
            page_sizes.append(
                {'class': 'selected' if size == self.size else '',
                 'url': self.build_url(size=size),
                 'size': size,
                 'last': False})

        if page_sizes:
            page_sizes[-1]['last'] = True
        return page_sizes

    def pages_items(self):

        visible_pages_, seperators = visible_pages(self.page, self.pages)

        items = []
        for number in xrange(1, self.pages + 1):
            if number in seperators:
                item = {'current': False,
                        'url': '',
                        'label': '...',
                        'class': '',
                        'seperator': True}
                items.append(item)

            item = {'current': self.page == number,
                    'url': self.build_url(page=number),
                    'label': str(number),
                    'class': 'hidden' if number not in visible_pages_ else '',
                    'seperator': False}
            items.append(item)

        return items

    def build_url(self, page=None, size=None, sort=None, facets=tuple(),
                  unselect_facets=tuple(), **kwargs):
        '''
        b/w compat
        '''
        query = MultiDict(request.params.items())
        query.update(kwargs)
        query[self.page_param] = page if page else 1
        query[self.size_param] = size if size else self.size
        query[self.sort_param] = sort if sort else self.selected_sort

        # sanitize the the query arguments
        query_items = ([(str(key), unicode(value).encode('utf-8')) for
                        (key, value) in query.items()])
        url_base = url.current(qualified=True)
        protocol = config.get('adhocracy.protocol', 'http').strip()
        if ', ' in url_base:
            # hard coded fix for enquetebeteiligung.de
            url_base = '%s://%s' % (protocol, url_base.split(', ')[1])
        else:
            url_base = '%s://%s' % (protocol, url_base.split('://')[1])
        return url_base + "?" + urllib.urlencode(query_items)

    def to_dict(self):
        return self.items

    def render_pager(self):
        '''
        render the template for the pager (without facets)
        '''
        return render_def('/pager.html', 'namedpager', pager=self)

    @property
    def sort_param(self):
        return "%s_sort" % self.name

    @property
    def size_param(self):
        return "%s_size" % self.name

    @property
    def page_param(self):
        return "%s_page" % self.name

    def __len__(self):
        return self.total_num_items()


# --[ sql based NamedPager ]------------------------------------------------

class NamedPager(PagerMixin):
    """
    A ``NamedPager`` is a list generator for the UI. The ``name`` is required
    in order to distinguish multiple pagers working on the same page.
    """

    def __init__(self, name, items, itemfunc, initial_size=10,
                 size=None, sorts={}, default_sort=None, enable_sorts=True,
                 enable_pages=True, **kwargs):
        self.name = name
        self._items = items
        self.itemfunc = itemfunc
        self.initial_size = initial_size
        if size is not None:
            self.size = size
        elif c.user and c.user.page_size:
            self.size = c.user.page_size
        else:
            self.size = initial_size
        self.sorts = sorts
        if len(sorts.values()):
            self.selected_sort = sorts.values().index(default_sort) + 1
        else:
            self.selected_sort = 0
        self.sorted = False
        self.enable_sorts = enable_sorts
        self.enable_pages = enable_pages
        self.kwargs = kwargs
        self._parse_request()

    def _parse_request(self):
        try:
            page_value = request.params.get(self.page_param)
            self.page = PAGE_VALIDATOR.to_python(page_value)
        except:
            self.page = 1

        try:
            size_value = request.params.get(self.size_param)
            self.size = SIZE_VALIDATOR.to_python(size_value)
        except:
            pass

        try:
            sort_validator = validators.Int(min=1, max=len(self.sorts.keys()),
                                            not_empty=True)
            sort_value = request.params.get(self.sort_param)
            self.selected_sort = sort_validator.to_python(sort_value)
        except:
            pass

    @property
    def items(self):
        if not self.sorted and len(self.sorts.values()):
            sorter = self.sorts.values()[self.selected_sort - 1]
            self._items = sorter(self._items)
            self.sorted = True
        return self._items[self.offset:self.offset + self.size]

    def total_num_items(self):
        return len(self._items)


def instances(instances):
    OLDEST = 'OLDEST'
    NEWEST = 'NEWEST'
    ACTIVITY = 'ACTIVITY'
    ALPHA = 'ALPHA'
    sorts = {
        OLDEST: sorting.entity_oldest,
        NEWEST: sorting.entity_newest,
        ACTIVITY: sorting.instance_activity,
        ALPHA: sorting.delegateable_label
    }
    CONFIG_KEY = 'adhocracy.listings.instance.sorting'
    configured_sort = config.get(CONFIG_KEY, ACTIVITY)
    if configured_sort not in sorts:
        configured_sort = ACTIVITY
    return NamedPager('instances', instances, tiles.instance.row,
                      sorts={_("oldest"): sorts[OLDEST],
                             _("newest"): sorts[NEWEST],
                             _("activity"): sorts[ACTIVITY],
                             _("alphabetically"): sorts[ALPHA]},
                      default_sort=sorts[configured_sort],
                      size=20)  # FIXME: hardcoded for enquetebeteiligung


def proposals(proposals, default_sort=None, **kwargs):
    if default_sort is None:
        default_sort = sorting.proposal_mixed
    sorts = {  # _("oldest"): sorting.entity_oldest,
             _("newest"): sorting.entity_newest,
             _("newest comment"): sorting.delegateable_latest_comment,
             _("most support"): sorting.proposal_support,
             _("mixed"): sorting.proposal_mixed,
             _("alphabetically"): sorting.delegateable_label}
    return NamedPager('proposals', proposals, tiles.proposal.row, sorts=sorts,
                      default_sort=default_sort, **kwargs)


def milestones(milestones, default_sort=None, **kwargs):
    if default_sort is None:
        default_sort = sorting.milestone_time
    sorts = {  # _("oldest"): sorting.entity_oldest,
             _("by date"): sorting.milestone_time,
             _("newest"): sorting.entity_newest,
             _("oldest"): sorting.entity_oldest,
             _("alphabetically"): sorting.delegateable_title}
    return NamedPager('milestones', milestones, tiles.milestone.row,
                      sorts=sorts, default_sort=default_sort, **kwargs)


def pages(pages, detail=True, default_sort=None, **kwargs):
    if default_sort is None:
        default_sort = sorting.hierarchical_title
    sorts = {_("newest"): sorting.entity_newest,
             _("most proposals"): sorting.norm_selections,
             _("alphabetically"): sorting.delegateable_title,
             _("hierarchical"): sorting.hierarchical_title}
    return NamedPager('pages', pages, tiles.page.row, sorts=sorts,
                    default_sort=default_sort, **kwargs)


def users(users, instance):
    activity_sorting = sorting.user_activity_factory(instance)
    sorts = {_("oldest"): sorting.entity_oldest,
             _("newest"): sorting.entity_newest,
             _("activity"): activity_sorting,
             _("alphabetically"): sorting.user_name}

    return NamedPager('users', users, tiles.user.row, sorts=sorts,
                      initial_size=15, default_sort=sorting.user_name)


def user_decisions(decisions):
    return NamedPager('decisions', decisions, tiles.decision.user_row,
                      sorts={_("oldest"): sorting.entity_oldest,
                             _("newest"): sorting.entity_newest},
                      default_sort=sorting.entity_newest)


def scope_decisions(decisions):
    return NamedPager('decisions', decisions, tiles.decision.scope_row,
                      sorts={_("oldest"): sorting.entity_oldest,
                             _("newest"): sorting.entity_newest},
                      default_sort=sorting.entity_newest)


def comments(comments):
    return NamedPager('comments', comments, tiles.comment.row,
                      sorts={_("oldest"): sorting.entity_oldest,
                             _("newest"): sorting.entity_newest},
                      default_sort=sorting.entity_newest)


def delegations(delegations):
    return NamedPager('delegations', delegations, tiles.delegation.row,
                      sorts={_("oldest"): sorting.entity_oldest,
                             _("newest"): sorting.entity_newest},
                      default_sort=sorting.entity_newest)


def events(events):
    return NamedPager('events', events, tiles.event.row)


def polls(polls, default_sort=None, **kwargs):
    if default_sort is None:
        default_sort = sorting.polls_time
    return NamedPager('polls', polls, tiles.poll.row,
                    default_sort=default_sort, **kwargs)


# --[ solr pager ]----------------------------------------------------------

class SolrIndexer(object):
    '''
    An indexer class to add information to the data
    which will be indexed in solr.
    '''

    @classmethod
    def add_data_to_index(cls, entity, data):
        """
        Add data from/based on *entity* to *data* which will be
        indexed in solr. Add information to it *data* or modify
        it. You don't need to return it.

        *entity*
           An :class:`adhocracy.model.meta.Indexable` object.
        *data*
           The data that will be send to solr.

        Return *None*
        """
        raise NotImplemented('has to be implemented in subclass')


class SolrFacet(SolrIndexer):
    """
    A Facet that can be used in searches.
    It's used like this:

    globally:
    >>> class SomeFacet(SolrFacet):
    ...     name = 'badge'
    ...     entity_type = Badge
    ...     title = u'Badge'

    Only in a thread:
    >>> some_facet = SomeFacet('mypager_prefix', request)
    >>> q = solr_query()
    >>> counts_query = q
    >>> # configure the query further
    >>> q, counts_query = some_facet.add_to_queries(q, counts_query)
    >>> response = q.execute()
    >>> counts_response = counts_response.execute()
    >>> some_facet.update(response, counts_response)
    >>> some_facet.items
    [...]
    """

    # overwrite in subclasses
    name = None
    entity_type = None
    title = None
    description = None
    solr_field = None
    show_empty = False
    show_current_empty = True
    template = '/pager.html'
    _response = None

    def __init__(self, param_prefix, request, **kwargs):
        # Translate the title and the description. We need to do that
        # during the request.
        self.title = self.title and _(self.title) or None
        self.description = self.description and _(self.description) or None
        self.param_prefix = param_prefix
        self.request = request
        self.request_key = "%s_facet" % param_prefix
        self.used = self._used(request)
        for (key, value) in kwargs.items():
            setattr(self, key, value)

    @property
    def response(self):
        if self._response is None:
            raise AssertionError('You have to .update() the facet first')
        return self._response

    @response.setter
    def response(self, response):
        self._response = response

    def add_to_queries(self, query, counts_query):
        '''
        Add the facet to the queries *query* and *counts_query*.
        The difference is that the *query* will be limited to facet values
        used in the the request.

        Returns: the modified queries as a (query, counts_query) tuple
        '''
        query = query.facet_by(self.solr_field)
        counts_query = counts_query.facet_by(self.solr_field)
        for value in self.used:
            query = query.query(**{self.solr_field: value})
        return query, counts_query

    def update(self, response, counts_response):
        '''
        Compute and update different attributes of the facet based
        on the solr *response* and the *base_query*.
        '''
        self.response = response
        self.counts_response = counts_response
        solr_field = self.solr_field

        # the counts in the current query which is limited to selected
        # facet values
        current_counts = response.facet_counts.facet_fields[solr_field]
        self.sorted_current_counts = sorted(current_counts,
                                            key=lambda(value, count): count,
                                            reverse=True)
        self.current_counts = dict(self.sorted_current_counts)

        # the counts in the current query which is limited to selected
        # facet values
        facet_counts = counts_response.facet_counts.facet_fields[solr_field]
        self.sorted_facet_counts = sorted(facet_counts,
                                          key=lambda(value, count): count,
                                          reverse=True)
        self.facet_counts = dict(self.sorted_facet_counts)

        self.current_items = self._current_items()

    # fixme: memoize
    def _facet_items(self, facet_counts):
        facet_items = []
        for (value, count) in facet_counts:
            facet_item = self._facet_item(value, count)
            if facet_item is not None:
                facet_items.append(facet_item)
        return self.sort_facet_items(facet_items)

    def _facet_item(self, value, count):
        '''
        Return an item dict for the facet *value*.
        *selected_values* is list of values used in the current
        query. count is the number of entries for this value in
        the current query results.
        '''
        item = self.get_item_data(value)
        if item is None:
            return None
        item['facet_count'] = count
        item['value'] = value
        return item

    def sort_facet_items(self, items):
        '''
        hook to sort the items facet specific. This is a
        generic that works with entities and sorts by entity title,
        name or id, or by facet_count. It is only sensible if all
        entities have the same attributes.
        '''

        return sorted(items, key=sort_key_getter)

    def available(self):
        if not self.response:
            return False
        return bool(len(self.current_items))

    def _used(self, request):
        used = []
        for param in request.params.getall(self.request_key):
            facet, value = param.split(':')
            if facet == self.name and value not in used:
                used.append(value)
        return used

    def _current_items(self):
        '''
        Return a list of facets to display.
        '''

        def show_facet(current_count, facet_count, show_empty,
                       show_current_empty):
            if show_empty:
                return True

            if current_count > 0 or show_current_empty:
                return True

            if facet_count > 0 or show_empty:
                return True

            return False

        ids = []
        facet_items = {}
        for (value, facet_count) in self.sorted_facet_counts:
            current_count = self.current_counts[value]

            if show_facet(current_count, facet_count,
                          self.show_empty, self.show_current_empty):
                id_ = value
                ids.append(id_)
                facet_items[id_] = {'current_count': current_count,
                                    'value': value}

        result = []

        entities = model.refs.get_entities(self.entity_type, ids)

        for entity in entities:
            item = facet_items[ref_attr_value(entity)]
            item['link_text'] = self.get_item_label(entity)
            item['disabled'] = (item['current_count'] == 0)
            item['selected'] = item['value'] in self.used
            item['url'] = self.get_item_url(item)

            result.append(item)

        return result

    def get_item_label(self, entity):
        for attribute in ['label', 'title', 'name']:
            if hasattr(entity, attribute):
                return getattr(entity, attribute)

        raise ValueError(('Could not find a label for facet '
                          '%s from entity %s') % (self.name, entity))

    def get_item_url(self, item):
        '''
        build a new url for the action when you click on it to
        select or unselect the item.
        '''
        values = self.used[:]
        if item['selected']:
            values.remove(item['value'])
        else:
            values.append(item['value'])
        return self.build_url(self.request, values)

    def unselect_all_link(self):
        '''
        return an url where no value for this facet is selected
        '''
        return self.build_url(self.request, [])

    def build_url(self, request, facet_values):
        '''
        Build an url from the *request* and the *facet_value*
        '''
        params = self.build_params(request, facet_values)
        url_base = url.current(qualified=True)
        protocol = config.get('adhocracy.protocol', 'http').strip()
        if ', ' in url_base:
            # hard coded fix for enquetebeteiligung.de
            url_base = '%s://%s' % (protocol, url_base.split(', ')[1])
        else:
            url_base = '%s://%s' % (protocol, url_base.split('://')[1])
        return url_base + "?" + urllib.urlencode(params)

    def build_params(self, request, facet_values):
        '''
        Build query parameters using the facet_values for this facet
        and the request.

        Returns: a list of (parameter, value) two-tuples
        '''
        params = MultiDict(request.params)

        # removing all ..._facet parameters and add them again
        current_facet_parameters = params.getall(self.request_key)
        if self.request_key in params:
            del params[self.request_key]

        # readd all _facet parameters not related to us
        for parameter in current_facet_parameters:
            name, value = parameter.split(':')
            if name != self.name:
                params.add(self.request_key, parameter)

        # add parameters for our facets
        facet_values = list(set(facet_values))
        for value in facet_values:
            params.add(self.request_key, "%s:%s" % (self.name, value))

        # sanitize and encode
        items = ([(str(key), unicode(value).encode('utf-8')) for
                  (key, value) in params.items()])
        return items

    def render(self):
        return render_def(self.template, 'facet', facet=self)


class UserBadgeFacet(SolrFacet):

    name = 'userbadge'
    entity_type = model.Badge
    title = u'Badge'
    solr_field = 'facet.badges'
    show_current_empty = False

    @classmethod
    def add_data_to_index(cls, user, index):
        if not isinstance(user, model.User):
            return
        index[cls.solr_field] = [ref_attr_value(badge) for
                                 badge in user.badges]


class InstanceBadgeFacet(SolrFacet):

    name = 'instancebadge'
    entity_type = model.Badge
    title = lazy_ugettext(u'Badge')
    solr_field = 'facet.instance.badges'
    show_current_empty = False

    @classmethod
    def add_data_to_index(cls, instance, index):
        if not isinstance(instance, model.Instance):
            return
        d = [ref_attr_value(badge) for badge in instance.badges]
        index[cls.solr_field] = d


class InstanceFacet(SolrFacet):

    name = 'instance'
    entity_type = model.Instance
    title = u'Projektgruppe'
    solr_field = 'facet.instances'

    @classmethod
    def add_data_to_index(cls, user, index):
        if not isinstance(user, model.User):
            return
        index[cls.solr_field] = [ref_attr_value(instance) for
                                 instance in user.instances]


class DelegateableBadgeCategoryFacet(SolrFacet):
    """Index all delegateable badge categories"""

    name = 'delegateablebadgecategory'
    entity_type = model.Badge
    title = u'Kategorien'  # FIXME: translate
    solr_field = 'facet.delegateable.badgecategory'
    show_current_empty = False

    @classmethod
    def add_data_to_index(cls, entity, data):
        if not isinstance(entity, model.Delegateable):
            return
        data[cls.solr_field] = [ref_attr_value(badge) for
                                badge in entity.categories]


class DelegateableBadgeFacet(SolrFacet):
    """Index all delegateable badges"""

    name = 'delegateablebadge'
    entity_type = model.Badge
    title = lazy_ugettext(u'Categories')
    solr_field = 'facet.delegateable.badge'
    show_current_empty = False

    @classmethod
    def add_data_to_index(cls, entity, data):
        if not isinstance(entity, model.Delegateable):
            return
        d = [ref_attr_value(badge) for badge in entity.badges]
        data[cls.solr_field] = d


class DelegateableAddedByBadgeFacet(SolrFacet):

    name = 'added_by_badge'
    entity_type = model.Badge
    title = lazy_ugettext(u'Created by')
    solr_field = 'facet.delegateable.added.by.badge'
    show_current_empty = False

    @classmethod
    def add_data_to_index(cls, entity, data):
        if not isinstance(entity, model.Delegateable):
            return
        data[cls.solr_field] = [ref_attr_value(badge) for
                                badge in entity.creator.badges if
                                (badge.instance is entity.instance or
                                 badge.instance is None)]


class DelegateableTags(SolrFacet):

    name = 'delegateabletags'
    entity_type = model.Tag
    title = lazy_ugettext(u'Tags')
    solr_field = 'facet.delegateable.tags'
    show_current_empty = False

    @classmethod
    def add_data_to_index(cls, entity, data):
        if not isinstance(entity, model.Delegateable):
            return
        tags = []
        for tag, count in entity.tags:
            tags.extend([ref_attr_value(tag)] * count)
        data[cls.solr_field] = tags


class DelegateableMilestoneFacet(SolrFacet):

    name = 'delegateablemilestone'
    entity_type = model.Milestone
    title = lazy_ugettext(u'Milestones')
    solr_field = 'facet.delegateable.milestones'
    show_current_empty = False

    @classmethod
    def add_data_to_index(cls, entity, data):
        if not isinstance(entity, model.Delegateable):
            return
        if entity.milestone is not None:
            data[cls.solr_field] = [entity.milestone.id]
        else:
            return []


class CommentOrderIndexer(SolrIndexer):

    solr_field = 'order.comment.order'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Comment):
            data[cls.solr_field] = sorting.comment_order_key(entity)


class CommentScoreIndexer(SolrIndexer):

    solr_field = 'order.comment.score'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Comment):
            data[cls.solr_field] = entity.poll.tally.score


class CommentScoreIndexer(SolrIndexer):

    solr_field = 'order.comment.score'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Comment):
            data[cls.solr_field] = entity.poll.tally.score


class NormNumSelectionsIndexer(SolrIndexer):

    solr_field = 'order.norm.num_selections'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if (isinstance(entity, model.Page) and
            entity.function == model.Page.NORM):
            data[cls.solr_field] = len(entity.selections)


class NormNumVariantsIndexer(SolrIndexer):

    solr_field = 'order.norm.selections'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if (isinstance(entity, model.Page) and
            entity.function == model.Page.NORM):
            data[cls.solr_field] = len(entity.selections)


class ProposalNumCommentsIndexer(SolrIndexer):

    solr_field = 'order.proposal.comments'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Delegateable):
            data[cls.solr_field] = len(entity.comments)


class ProposalNewestCommentsIndexer(SolrIndexer):

    solr_field = 'order.newestcomment'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Proposal):
            if entity.comment_count() > 0:
                commenttime = entity.find_latest_comment_time()
                value = time.mktime(commenttime.timetuple())
                data[cls.solr_field] = value


class ProposalSupportIndexer(SolrIndexer):

    solr_field = 'order.proposal.support'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Proposal):
            data[cls.solr_field] = entity.rate_poll.tally.score


class ProposalVotesIndexer(SolrIndexer):

    solr_field = 'order.proposal.votes'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Proposal):
            tally = entity.rate_poll.tally
            data[cls.solr_field] = tally.num_for + tally.num_against


class ProposalVotesYesIndexer(SolrIndexer):

    solr_field = 'order.proposal.yesvotes'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Proposal):
            tally = entity.rate_poll.tally
            data[cls.solr_field] = tally.num_for


class ProposalVotesNoIndexer(SolrIndexer):

    solr_field = 'order.proposal.novotes'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Proposal):
            tally = entity.rate_poll.tally
            data[cls.solr_field] = tally.num_against


class ProposalMixedIndexer(SolrIndexer):

    solr_field = 'order.proposal.mixed'

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.Proposal):
            data[cls.solr_field] = sorting.proposal_mixed_key(entity)


class InstanceUserActivityIndexer(SolrIndexer):

    @classmethod
    def solr_field(cls, instance=None):
        field = 'order.user.activity'
        if instance is not None:
            field = field + '.%s' % instance.key
        return field

    @classmethod
    def add_data_to_index(cls, entity, data):
        if isinstance(entity, model.User):
            activity_sum = 0
            for instance in entity.instances:
                activity = user_activity(instance, entity)
                data[cls.solr_field(instance)] = activity
                activity_sum = activity_sum + activity
            data[cls.solr_field()] = activity_sum


class SolrPager(PagerMixin):
    '''
    An pager currently compatible to :class:`adhocracy.lib.pager.NamedPager`.
    '''

    def __init__(self, name, itemfunc, entity_type=None, extra_filter=None,
                 initial_size=20, size=None, sorts=None,
                 enable_sorts=True, enable_pages=True, facets=tuple(),
                 wildcard_queries=None):
        self.name = name
        self.itemfunc = itemfunc
        self.enable_pages = enable_pages
        self.extra_filter = extra_filter
        self.facets = [Facet(self.name, request) for Facet in facets]
        self.wildcard_queries = wildcard_queries or {}
        self.initial_size = initial_size
        if size is not None:
            self.size = size
        elif c.user and c.user.page_size:
            self.size = c.user.page_size
        else:
            self.size = initial_size
        self.size = self._get_size()

        self.enable_sorts = enable_sorts
        self.sorts = sorts
        self.sorts.set_pager(pager=self)
        if self.sorts:
            self.selected_sort = self.sorts.selected().value

        self.page = self._get_page()

        ## build the query
        query = sunburnt_query(entity_type)
        if self.extra_filter:
            query = query.filter(**self.extra_filter)
        for field, string in self.wildcard_queries.items():
            query = add_wildcard_query(query, field, string)

        # Add facets
        counts_query = query
        counts_query = counts_query.paginate(rows=0)
        query.faceter.update(limit='65000')
        counts_query.faceter.update(limit='65000')
        for facet in self.facets:
            query, counts_query = facet.add_to_queries(query, counts_query)

        # Add pagination and sorting
        if enable_pages:
            query = query.paginate(start=self.offset, rows=self.size)

        if self.selected_sort is not None:
            query = query.sort_by(self.selected_sort)

        # query solr and calculate values from it
        self.response = query.execute()
        self.counts_response = counts_query.execute()
        # if we are out of the page range do a permanent redirect
        # to the last page
        if (self.pages > 0) and (self.page > self.pages):
            new_url = self.build_url(page=self.pages)
            redirect(new_url, code=301)

        for facet in self.facets:
            facet.update(self.response, self.counts_response)
        self.items = self._items_from_response(self.response)

    def total_num_items(self):
        '''
        return the total numbers of results
        '''
        return self.response.result.numFound

    def _items_from_response(self, response):
        '''
        Get model objects form the documents returned
        in the solr *response*.
        '''
        items = []
        if not response.result.numFound:
            return items

        # Don't use entity_type.find_all() cause
        # it won't preserve the order of items.
        refs = [doc['ref'] for doc in response.result.docs]
        entities = model.refs.to_entities(refs)
        return entities

    def _get_page(self):
        page = 1
        try:
            page_value = request.params.get(self.page_param)
            page = PAGE_VALIDATOR.to_python(page_value)
        finally:
            return page

    def _get_size(self):

        size = self.size
        try:
            size_value = request.params.get(self.size_param)
            size = SIZE_VALIDATOR.to_python(size_value)
        finally:
            return size

    def render_facets(self):
        '''
        render all facets
        '''
        return render_def('/pager.html', 'facets', pager=self)


class SortOption(object):

    def __init__(self, value, label, old=None, func=None, description=None):
        self.value = value
        self.label = label
        self.old = old
        self.func = func
        self.description = description

    def __call__(self, **kwargs):
        '''
        Factory to return a modified copy of self.
        '''
        value = kwargs.get('value', self.value)
        label = kwargs.get('label', self.label)
        old = kwargs.get('old', self.old)
        func = kwargs.get('func', self.func)
        description = kwargs.get('description', self.description)
        return SortOption(value, label, old=old, func=func,
                          description=description)

    def __eq__(self, other):
        return self.value == other.value


class NamedSort(object):

    pager = None

    def __init__(self, sortoptions=tuple(), default=None,
                 template='/pager.html', mako_def="sort_dropdown"):
        '''
        *sortsoptions* (iterable)
            An list of (<groupname>, <optionslist>) tuples where
            <optionslist> itself is a list of :class:`SortOption` s.
        *default* (:class:`SortOption`)
            A :class:`SortOption` object for the default sort.
        *template* (str)
            The (mako) Template used to render the sort options.
        *mako_def* (str)
            The name of the make def to use.
        '''
        self.by_value = {}
        self.by_old = {}
        self.by_group = {}
        self.groups = []
        for (group_label, optionslist) in sortoptions:
            self.add_group(group_label, optionslist)

        # set the default
        if default is not None:
            assert default.value in self.by_value
            self._default = default.value

        self.template = template
        self.mako_def = mako_def

    @property
    def default(self):
        if self._default in self.by_value:
            return self.by_value[self._default]
        else:
            return self.by_group[self.groups[0]][0]

    def current_value(self):
        return request.params.get(self.pager.sort_param)

    def selected(self):
        value = self.current_value()

        if value is None:
            return self.default

        try:
            return self.by_value[value]
        except KeyError:
            try:
                new_value = self.by_old[value].value
                redirect(self.pager.build_url(sort=new_value), code=301)
            except KeyError:
                redirect(self.pager.build_url(sort=self.default, code=301))

    def add_group(self, label, options):
        assert (label not in self.groups), 'We do not support changing groups'
        self.groups.append(label)
        self.by_group[label] = options
        for option in options:
            assert isinstance(option, SortOption)
            assert option.value not in self.by_value
            self.by_value[option.value] = option
            if option.old is not None:
                assert option.old not in self.by_old
                self.by_old[option.old] = option

    def set_pager(self, pager):
        self.pager = pager

    def render(self):
        return render_def(self.template, self.mako_def, sorts=self)

    def grouped_options(self):
        return [(group, self.by_group[group]) for group in self.groups]

    def __len__(self):
        return len(self.by_value.keys())


OLDEST = SortOption('+create_time', L_("Oldest"))
NEWEST = SortOption('-create_time', L_("Newest"))
NEWEST_COMMENT = SortOption('-order.newestcomment', L_("Newest Comment"))
ACTIVITY = SortOption('-activity', L_("Activity"))
ALPHA = SortOption('order.title', L_("Alphabetically"))
PROPOSAL_SUPPORT = SortOption('-order.proposal.support', L_("Most Support"),
                              description=L_('Yays - nays'))
PROPOSAL_VOTES = SortOption('-order.proposal.votes', L_("Most Votes"),
                              description=L_('Yays + nays'))
PROPOSAL_YES_VOTES = SortOption('-order.proposal.yesvotes', L_("Most Ayes"))
PROPOSAL_NO_VOTES = SortOption('-order.proposal.novotes', L_("Most Nays"))
PROPOSAL_MIXED = SortOption('-order.proposal.mixed', L_('Mixed'),
                              description=L_('Age and Support'))

USER_SORTS = NamedSort([[None, (OLDEST(old=1),
                                NEWEST(old=2),
                                ACTIVITY(old=3),
                                ALPHA(old=4))]],
                       default=ACTIVITY,
                       mako_def="sort_dropdown")


INSTANCE_SORTS = NamedSort([[None, (OLDEST(old=1),
                                NEWEST(old=2),
                                ACTIVITY(old=3),
                                ALPHA(old=4))]],
                                default=ACTIVITY,
                       mako_def="sort_dropdown")


PROPOSAL_SORTS = NamedSort([[L_('Support'), (PROPOSAL_SUPPORT(old=2),
                                             PROPOSAL_VOTES,
                                             PROPOSAL_YES_VOTES,
                                             PROPOSAL_NO_VOTES)],
                            [L_('Date'), (NEWEST(old=1,
                                                 label=L_('Newest Proposals')),
                                          NEWEST_COMMENT)],
                            [L_('Other'), (ALPHA(old=4),
                                           PROPOSAL_MIXED(old=3))]],
                           default=PROPOSAL_MIXED,
                           mako_def="sort_slidedown")


def solr_instance_users_pager(instance):
    extra_filter = {'facet.instances': instance.key}
    pager = SolrPager('users', tiles.user.row,
                      entity_type=model.User,
                      sorts=USER_SORTS,
                      extra_filter=extra_filter,
                      facets=[UserBadgeFacet])
    return pager


def solr_global_users_pager():
    pager = SolrPager('users', tiles.user.row,
                      entity_type=model.User,
                      sorts=USER_SORTS,
                      facets=[UserBadgeFacet, InstanceFacet]
                      )
    return pager


def solr_instance_pager():
    # override default sort
    # TODO: is paging working? [joka]
    custom_default = config.get('adhocracy.listings.instance.sorting')
    sorts = {"ALPHA": ALPHA,
             "ACTIVITY": ACTIVITY,
             "NEWEST": NEWEST,
             "OLDEST": OLDEST,}
    instance_sorts = copy.copy(INSTANCE_SORTS)
    if custom_default and custom_default in sorts:
        instance_sorts._default = sorts[custom_default].value
    # create pager
    pager = SolrPager('instances', tiles.instance.row,
                      entity_type=model.Instance,
                      sorts=instance_sorts,
                      facets=[InstanceBadgeFacet],
                      )
    return pager


def solr_proposal_pager(instance, wildcard_queries=None):
    extra_filter = {'instance': instance.key}
    pager = SolrPager('proposals', tiles.proposal.row,
                      entity_type=model.Proposal,
                      sorts=PROPOSAL_SORTS,
                      extra_filter=extra_filter,
                      facets=[DelegateableBadgeCategoryFacet,
                              DelegateableBadgeFacet,
                              DelegateableAddedByBadgeFacet,
                              DelegateableTags,
                              DelegateableMilestoneFacet],
                      wildcard_queries=wildcard_queries)
    return pager


INDEX_DATA_FINDERS = [v for v in globals().values() if
                      (isclass(v) and issubclass(v, SolrIndexer) and
                      ((v is not SolrFacet) and (v is not SolrIndexer)))]
