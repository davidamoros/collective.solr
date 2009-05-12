from logging import getLogger
from time import time, clock, strftime
from zope.interface import implements
from zope.component import queryUtility
from Products.Five.browser import BrowserView
from Products.CMFCore.utils import getToolByName
from httplib import BadStatusLine

from collective.solr.interfaces import ISolrConnectionManager
from collective.solr.interfaces import ISolrIndexQueueProcessor
from collective.solr.interfaces import ISolrMaintenanceView
from collective.solr.interfaces import ISearch
from collective.solr.indexer import indexable
from collective.solr.utils import findObjects

logger = getLogger('collective.solr.maintenance')


def timer(func=time):
    """ set up a generator returning the elapsed time since the last call """
    def gen(last=func()):
        while True:
            elapsed = func() - last
            last = func()
            yield '%.3fs' % elapsed
    return gen()


def checkpointIterator(function, interval=100):
    """ the iterator will call the given function for every nth invocation """
    counter = 0
    while True:
        counter += 1
        if counter % interval == 0:
            function()
        yield None


class SolrMaintenanceView(BrowserView):
    """ helper view for indexing all portal content in Solr """
    implements(ISolrMaintenanceView)

    def mklog(self):
        """ helper to prepend a time stamp to the output """
        write = self.request.RESPONSE.write
        def log(msg, timestamp=True):
            if timestamp:
                msg = strftime('%Y/%m/%d-%H:%M:%S ') + msg
            write(msg)
        return log

    def optimize(self):
        """ optimize solr indexes """
        manager = queryUtility(ISolrConnectionManager)
        manager.getConnection(timeout=None).commit(optimize=True)
        return 'solr indexes optimized.'

    def clear(self):
        """ clear all data from solr, i.e. delete all indexed objects """
        manager = queryUtility(ISolrConnectionManager)
        uniqueKey = manager.getSchema().uniqueKey
        conn = manager.getConnection(timeout=None)
        conn.deleteByQuery('%s:[* TO *]' % uniqueKey)
        conn.commit()
        return 'solr index cleared.'

    def reindex(self, batch=100, skip=0, cache=1000, attributes=None):
        """ find all contentish objects (meaning all objects derived from one
            of the catalog mixin classes) and (re)indexes them """
        manager = queryUtility(ISolrConnectionManager)
        manager.setTimeout(None)        # don't time out during reindexing
        proc = queryUtility(ISolrIndexQueueProcessor, name='solr')
        log = self.mklog()
        if skip:
            log('skipping indexing of %d object(s)...\n' % skip)
        now, cpu = time(), clock()
        count = 0
        indexed = 0
        commit = batch
        for path, obj in findObjects(self.context):
            if indexable(obj):
                count += 1
                if count <= skip:
                    continue
                log('indexing %r' % obj)
                lap = time()
                try:
                    proc.index(obj, attributes)
                    indexed += 1
                except BadStatusLine:
                    log('WARNING: error while indexing %r' % obj)
                    logger.exception('error while indexing %r', obj)
                    manager.getConnection().reset()     # force new connection
                log(' (%.4fs)\n' % (time() - lap), timestamp=False)
                commit -= 1
                if commit == 0:
                    msg = 'intermediate commit: %d objects indexed in %.4fs\n'
                    log(msg % (indexed, time() - now))
                    proc.commit(wait=True)
                    commit = batch
                    manager.getConnection().reset()     # force new connection
                    if cache:
                        db = self.context.getPhysicalRoot()._p_jar.db()
                        size = db.cacheSize()
                        if size > cache:
                            msg = 'minimizing zodb cache with %d objects...\n'
                            log(msg % size)
                            db.cacheMinimize()
        proc.commit(wait=True)      # make sure to commit in the end...
        now, cpu = time() - now, clock() - cpu
        log('solr index rebuilt.\n')
        msg = 'indexed %d object(s) in %.3f seconds (%.3f cpu time).'
        msg = msg % (indexed, now, cpu)
        log(msg)
        logger.info(msg)

    def metadata(self, index, key, func=lambda x: x):
        """ build a mapping between a unique key and a given attribute from
            the portal catalog; catalog metadata must exist for the given
            index """
        catalog = getToolByName(self.context, 'portal_catalog')
        cat = catalog._catalog      # get the real catalog...
        pos = cat.schema[index]
        data = {}
        for uid, rids in cat.getIndex(key).items():
            for rid in rids:
                value = cat.data[rid][pos]
                if value is not None:
                    data[uid] = func(value)
        return data

    def diff(self):
        """ determine objects that need to be indexed/reindex/unindexed by
            diff'ing the records in the portal catalog and solr """
        key = queryUtility(ISolrConnectionManager).getSchema().uniqueKey
        uids = self.metadata('modified', key=key, func=lambda x: x.millis())
        search = queryUtility(ISearch)
        reindex = []
        unindex = []
        rows = len(uids) * 10               # sys.maxint makes solr choke :(
        query = '%s:[* TO *]' % key
        for flare in search(query, rows=rows, fl='%s modified' % key):
            uid = getattr(flare, key)
            assert uid, 'empty unique key?'
            if uid in uids:
                if uids[uid] > flare.modified.millis():
                    reindex.append(uid)     # item isn't current
                del uids[uid]               # remove from the list in any case
            else:
                unindex.append(uid)         # item doesn't exist in catalog
        index = uids.keys()
        return index, reindex, unindex

    def sync(self, batch=100, cache=1000):
        """ sync the solr index with the portal catalog;  records contained
            in the catalog but not in solr will be indexed and records not
            contained in the catalog can be optionally removed;  this can
            be used to ensure consistency between zope and solr after the
            solr server has been unavailable etc """
        manager = queryUtility(ISolrConnectionManager)
        manager.setTimeout(None)        # don't time out during syncing
        proc = queryUtility(ISolrIndexQueueProcessor, name='solr')
        db = self.context.getPhysicalRoot()._p_jar.db()
        log = self.mklog()
        real = timer()          # real time
        lap = timer()           # real lap time (for intermediate commits)
        cpu = timer(clock)      # cpu time
        log('determining differences between portal catalog and solr...')
        index, reindex, unindex = self.diff()
        log(' (%s).\n' % lap.next(), timestamp=False)
        log('operations needed: %d "index", %d "reindex", %d "unindex"\n' % (
            len(index), len(reindex), len(unindex)))
        processed = 0
        def checkPoint():
            log('intermediate commit (%d objects processed, '
                'last batch in %s)...\n' % (processed, lap.next()))
            proc.commit(wait=True)
            manager.getConnection().reset()     # force new connection
            if cache:
                size = db.cacheSize()
                if size > cache:
                    log('minimizing zodb cache with %d objects...\n' % size)
                    db.cacheMinimize()
        single = timer()        # real time for single object
        cpi = checkpointIterator(checkPoint, batch)
        lookup = getToolByName(self.context, 'reference_catalog').lookupObject
        log('processing %d "index" operations next...\n' % len(index))
        for uid in index:
            obj = lookup(uid)
            if indexable(obj):
                log('indexing %r' % obj)
                proc.index(obj)
                processed += 1
                log(' (%s).\n' % single.next(), timestamp=False)
                cpi.next()
                single.next()   # don't count commit time here...
            else:
                log('not indexing unindexable object %r.\n' % obj)
        log('processing %d "reindex" operations next...\n' % len(reindex))
        for uid in reindex:
            obj = lookup(uid)
            if indexable(obj):
                log('reindexing %r' % obj)
                proc.reindex(obj)
                processed += 1
                log(' (%s).\n' % single.next(), timestamp=False)
                cpi.next()
                single.next()   # don't count commit time here...
            else:
                log('not reindexing unindexable object %r.\n' % obj)
        log('processing %d "unindex" operations next...\n' % len(unindex))
        conn = proc.getConnection()
        for uid in unindex:
            obj = lookup(uid)
            if obj is None:
                log('unindexing %r' % uid)
                conn.delete(id=uid)
                processed += 1
                log(' (%s).\n' % single.next(), timestamp=False)
                cpi.next()
                single.next()   # don't count commit time here...
            else:
                log('not unindexing existing object %r (%r).\n' % (obj, uid))
        proc.commit(wait=True)      # make sure to commit in the end...
        log('solr index synced.\n')
        msg = 'processed %d object(s) in %s (%s cpu time).'
        msg = msg % (processed, real.next(), cpu.next())
        log(msg)
        logger.info(msg)

    def catalogSync(self, index, batch=100):
        """ add or sync a single solr index using data from the portal
            catalog;  existing data in solr will be overwritten for the
            given index """
        manager = queryUtility(ISolrConnectionManager)
        manager.setTimeout(None)        # don't time out during syncing
        log = self.mklog()
        log('getting data for "%s" from portal catalog...', index)
        key = manager.getSchema().uniqueKey
        data = self.metadata(index, key=key)
        log('syncing "%s" from portal catalog to solr...', index)
        real = timer()          # real time
        lap = timer()           # real lap time (for intermediate commits)
        cpu = timer(clock)      # cpu time
        processed = 0
        conn = manager.getConnection()
        def checkPoint():
            log('intermediate commit (%d items processed, '
                'last batch in %s)...\n' % (processed, lap.next()))
            conn.commit()
            manager.getConnection().reset()     # force new connection
        cpi = checkpointIterator(checkPoint, batch)
        for uid, value in data.items():
            data = {key: uid, index: value}
            conn.add(**data)
            cpi.next()
        conn.commit()           # make sure to commit in the end...
        log('portal catalog data synced.\n')
        msg = 'processed %d items in %s (%s cpu time).'
        msg = msg % (processed, real.next(), cpu.next())
        log(msg)
        logger.info(msg)
