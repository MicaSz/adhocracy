import logging
from time import time
from itertools import chain

from sources import watchlist_source, vote_source, instance_source, comment_source, delegation_source
from filters import self_filter, duplicates_filter
from sinks import log_sink, mail_sink, twitter_sink

log = logging.getLogger(__name__)

def notify(event):
    log.debug("Event notification processing: %s" % event)
    begin_time = time()
    sources = filter(lambda g: g, [watchlist_source(event),
                                   vote_source(event),
                                   instance_source(event),
                                   delegation_source(event),
                                   comment_source(event)])
    pipeline = chain(*sources)
    pipeline = log_sink(pipeline)
    
    pipeline = self_filter(pipeline)
    pipeline = duplicates_filter(pipeline)
    
    pipeline = log_sink(pipeline)
    pipeline = twitter_sink(pipeline)
    pipeline = mail_sink(pipeline)
    
    for _ in pipeline: pass
    
    end_time = time() - begin_time
    log.debug("-> processing took: %sms" % (end_time * 1000)) 

        