# -*- coding: utf-8 -*-

#################################################################################################

import json
import logging
import Queue
import threading
import os

import xbmc
import xbmcvfs

from libraries import requests
from helper.utils import should_stop, delete_build
from helper import settings, stop, event, window
from emby import Emby
from emby.core import api
from emby.core.exceptions import HTTPException

#################################################################################################

LOG = logging.getLogger("EMBY."+__name__)
LIMIT = min(int(settings('limitIndex') or 50), 50)

#################################################################################################

def get_embyserver_url(handler):

    if handler.startswith('/'):

        handler = handler[1:]
        LOG.warn("handler starts with /: %s", handler)

    return  "{server}/emby/%s" % handler

def browse_info():
    return  (  
                "DateCreated,EpisodeCount,SeasonCount,Path,Genres,Studios,Taglines,MediaStreams,Overview,Etag,"
                "ProductionLocations,Width,Height,RecursiveItemCount,ChildCount"
            )

def _http(action, url, request={}, server_id=None):
    request.update({'url': url, 'type': action})
    
    return Emby(server_id)['http/request'](request)

def _get(handler, params=None, server_id=None):
    return  _http("GET", get_embyserver_url(handler), {'params': params}, server_id)

def _post(handler, json=None, params=None, server_id=None):
    return  _http("POST", get_embyserver_url(handler), {'params': params, 'json': json}, server_id)

def _delete(handler, params=None, server_id=None):
    return  _http("DELETE", get_embyserver_url(handler), {'params': params}, server_id)

def validate_view(library_id, item_id):

    ''' This confirms a single item from the library matches the view it belongs to.
        Used to detect grouped libraries.
    '''
    try:
        result = _get("Users/{UserId}/Items", {
                    'ParentId': library_id,
                    'Recursive': True,
                    'Ids': item_id
                 })
    except Exception:
        return False

    return True if len(result['Items']) else False

def get_single_item(parent_id, media):
    return  _get("Users/{UserId}/Items", {
                'ParentId': parent_id,
                'Recursive': True,
                'Limit': 1,
                'IncludeItemTypes': media
            })

def get_filtered_section(parent_id, media=None, limit=None, recursive=None, sort=None, sort_order=None,
                         filters=None, server_id=None):

    ''' Get dynamic listings.
    '''
    params = {
        'ParentId': parent_id,
        'IncludeItemTypes': media,
        'IsMissing': False,
        'Recursive': recursive if recursive is not None else True,
        'Limit': limit,
        'SortBy': sort or "SortName",
        'SortOrder': sort_order or "Ascending",
        'Filters': filters,
        'ImageTypeLimit': 1,
        'IsVirtualUnaired': False,
        'CollapseBoxSetItems': not settings('groupedSets.bool'),
        'Fields': browse_info()
    }
    if settings('getCast.bool'):
        params['Fields'] += ",People"

    if media and 'Photo' in media:
        params['Fields'] += ",Width,Height"

    return  _get("Users/{UserId}/Items", params, server_id)

def get_movies_by_boxset(boxset_id, server_id=None):

    for items in get_items(boxset_id, "Movie", server_id=server_id):
        yield items

def get_episode_by_show(show_id, server_id=None):

    for items in get_items(show_id, "Episode", server_id=server_id):
        yield items

def get_items(parent_id, item_type=None, basic=False, params=None, server_id=None):

    query = {
        'url': "Users/{UserId}/Items",
        'params': {
            'ParentId': parent_id,
            'IncludeItemTypes': item_type,
            'SortBy': "SortName",
            'SortOrder': "Ascending",
            'Fields': api.basic_info() if basic else api.info()
        }
    }

    if params:
        query['params'].update(params)

    for items in _get_items(query, server_id):
        yield items

def get_artists(parent_id=None, basic=False, params=None, server_id=None):

    query = {
        'url': "Artists",
        'params': {
            'UserId': "{UserId}",
            'ParentId': parent_id,
            'SortBy': "SortName",
            'SortOrder': "Ascending",
            'Fields': api.basic_info() if basic else api.music_info()
        }
    }

    if params:
        query['params'].update(params)

    for items in _get_items(query, server_id):
        yield items

def get_albums_by_artist(artist_id, basic=False, server_id=None):

    params = {
        'SortBy': "DateCreated",
        'ArtistIds': artist_id
    }
    for items in get_items(None, "MusicAlbum", basic, params, server_id):
        yield items

@stop()
def _get_items(query, server_id=None):

    ''' query = {
            'url': string,
            'params': dict -- opt, include StartIndex to resume
        }
    '''
    items = {
        'Items': [],
        'TotalRecordCount': 0,
        'RestorePoint': {}
    }

    url = query['url']
    params = query.get('params', {})
    params.update({
        'CollapseBoxSetItems': False,
        'IsVirtualUnaired': False,
        'EnableTotalRecordCount': False,
        'LocationTypes': "FileSystem,Remote,Offline",
        'IsMissing': False,
        'Recursive': True
    })

    try:
        test_params = dict(params)
        test_params['Limit'] = 1
        test_params['EnableTotalRecordCount'] = True

        items['TotalRecordCount'] = _get(url, test_params, server_id=server_id)['TotalRecordCount']

    except Exception as error:
        LOG.error("Failed to retrieve the server response %s: %s params:%s", url, error, params)

    else:
        index = params.get('StartIndex', 0)
        total = items['TotalRecordCount']

        while index < total:

            params['StartIndex'] = index
            params['Limit'] = LIMIT
            result = _get(url, params, server_id=server_id)

            items['Items'].extend(result['Items'])
            items['RestorePoint'] = query
            yield items

            del items['Items'][:]
            index += LIMIT

class GetItemWorker(threading.Thread):

    is_done = False

    def __init__(self, server, queue, output):

        self.server = server
        self.queue = queue
        self.output = output
        threading.Thread.__init__(self)

    def run(self):

        with requests.Session() as s:
            while True:

                try:
                    item_id = self.queue.get(timeout=1)
                except Queue.Empty:

                    self.is_done = True
                    LOG.info("--<[ q:download/%s ]", id(self))

                    return

                request = {'type': "GET", 'handler': "Users/{UserId}/Items/%s" % item_id}
                try:
                    result = self.server['http/request'](request, s)

                    if result['Type'] in self.output:
                        self.output[result['Type']].put(result)
                except HTTPException as error:
                    LOG.error("--[ http status: %s ]", error.status)

                    if error.status != 500: # to retry
                        continue

                except Exception as error:
                    LOG.exception(error)

                self.queue.task_done()

                if xbmc.Monitor().abortRequested():
                    break

class TheVoid(object):

    def __init__(self, method, data):

        ''' This will block until response is received.
            This is meant to go as fast as possible, a response will always be returned.
        '''
        if type(data) != dict:
            raise Exception("unexpected data format")

        data['VoidName'] = id(self)
        LOG.info("---[ contact mothership/%s ]", method)
        LOG.debug(data)

        event(method, data)
        self.method = method
        self.data = data

    def get(self):

        while True:
            response = window('emby_%s.json' % self.data['VoidName'])

            if response != "":

                LOG.debug("--<[ beacon/emby_%s.json ]", self.data['VoidName'])
                window('emby_%s' % self.data['VoidName'], clear=True)

                return response

            if xbmc.Monitor().abortRequested():
                break

def get_objects(src, filename):

    ''' Download objects dependency to temp cache folder.
    '''
    temp = xbmc.translatePath('special://temp/emby/').decode('utf-8')

    if not xbmcvfs.exists(temp):
        xbmcvfs.mkdir(temp)
    else:
        delete_build()

    path = os.path.join(temp, filename)
    try:
        response = requests.get(src, stream=True)
        response.raise_for_status()
    except Exception as error:
        raise
    else:
        with open(path, 'wb') as f:
            f.write(response.content)
            del response

    xbmc.executebuiltin('Extract(%s, %s)' % (path, temp))
    xbmcvfs.delete(path)
