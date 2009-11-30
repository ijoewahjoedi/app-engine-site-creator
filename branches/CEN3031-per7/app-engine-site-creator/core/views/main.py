#!/usr/bin/python2.5
#
# Copyright 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Main views for viewing pages and downloading files."""

import datetime
import logging
import mimetypes
import yaml

import configuration
from google.appengine.api import users
from google.appengine.ext.webapp import Response
from django import http
from django.http import HttpResponse, Http404
from django.core import urlresolvers
from django.utils import simplejson
from django.http import Http404
from core.models.sidebar import Sidebar
from core.models.files import Page, File, FileStore
from core.models.users import Theme
from core import utility


def send_page(page, request):
    """Sends a given page to a user if they have access rights.

    Args:
      page: The page to send to the user
      request: The Django request object

    Returns:
      A Django HttpResponse containing the requested page, or 
      an error message.

    """
    profile = request.profile
    global_access = page.acl.global_read
    if not global_access:
        if profile is None:
            return http.HttpResponseRedirect(
                users.create_login_url(
                    request.path))
        if not page.user_can_read(profile):
            logging.warning('User %s made an invalid attempt to access'
                            'page %s' % (profile.email, page.name))
            return HttpResponse(status=403)

    files = page.attached_files()
    files = [file_obj for file_obj in files if not file_obj.is_hidden]

    for item in files:
        ext = item.name.split('.')[-1]
        item.icon = '/static/images/fileicons/%s.png' % ext

    pageversions = Page.all().filter('name =', page.name).filter('parent_page =', page.parent_page).order('version')
    minversion = pageversions[0].version
    maxversion = pageversions[len(pageversions)-1].version
    discretevals = maxversion - minversion + 1
    
    is_editor = page.user_can_write(profile)

    if Theme.get_theme():
        template = 'themes/%s/page.html' % (Theme.get_theme().name)
        if Theme.get_theme().name == 'frames' and (page.path == "" or page.path == "/"):
            return utility.respond(request,'themes/frames/base')
    elif configuration.SYSTEM_THEME_NAME:
        template = 'themes/%s/page.html' % (configuration.SYSTEM_THEME_NAME)

    return utility.respond(request, template, {'page': page, 'files': files, 'version': page.version, 'pageversions': pageversions,
                                               'maxversion': maxversion, 'minversion': minversion,  'discretevals': discretevals, 
                                               'is_editor': is_editor})


def send_file(file_record, request):
    """Sends a given file to a user if they have access rights.

    Args:
      file_record: The file to send to the user
      request: The Django request object

    Returns:
      A Django HttpResponse containing the requested file, or an error message.

    """
    profile = request.profile
    mimetype = mimetypes.guess_type(file_record.name)[0]

    if not file_record.user_can_read(profile):
        logging.warning('User %s made an invalid attempt to access file %s' %
                        (profile.email, file_record.name))
        return HttpResponse(status=403)

    expires = datetime.datetime.now() + configuration.FILE_CACHE_TIME
    response = http.HttpResponse(content=file_record.data, mimetype=mimetype)
    response['Cache-Control'] = configuration.FILE_CACHE_CONTROL
    response['Expires'] = expires.strftime('%a, %d %b %Y %H:%M:%S GMT')
    return response


def get_url(request, path_str, version_num=None):
    """Parse the URL and return the requested content to the user.

    Args:
      request: The Django request object.
      path_str: The URL path as a string

    Returns:
      A Django HttpResponse containing the requested page or file, or an
      error message.

    """
    def follow_url_forwards(base, path):
        """Follow the path forwards, returning the desired item."""
        logging.debug('Base: %s\nPath: %s', base, path)
        if not base:
            return None
        if not path or path == ['']:
            if version_num:
                req_version_page = Page.all().filter('name =', base.name).filter('parent_page =', base.parent_page).filter('version =', int(version_num)).get()
                if req_version_page:
                    base.content = req_version_page.content
                    base.version = req_version_page.version
                    utility.memcache_set(('path:%sv:%s') % (path_str, version_num), base)
                    logging.debug('set memcache, returning %s', base)
            else:
                utility.memcache_set('path:%s' % path_str, base)
                logging.debug('set memcache, returning %s', base)
            return base
        if len(path) == 1:
            attachment = base.get_attachment(path[0])
            if attachment:
                return attachment
        return follow_url_forwards(base.get_child(path[0]), path[1:])

    def follow_url_backwards(pre_path, post_path):
        """Traverse the path backwards to find a cached page or the 
           root."""
        logging.debug('pre_path: %s post_path: %s', pre_path, post_path)
        if version_num:
            key = 'path:' + '/'.join(pre_path) + 'v:' + version_num
        else:
            key = 'path:' + '/'.join(pre_path)
        item = utility.memcache_get(key)
        if item:
            return follow_url_forwards(item, post_path)
        if not pre_path:
            return follow_url_forwards(Page.get_root(), post_path)
        return follow_url_backwards(pre_path[:-1], [pre_path[-1]] + post_path)

    path = [dir_name for dir_name in path_str.split('/') if dir_name is not '']
    logging.debug('path = %s version_num = %s',path,version_num)
    item = follow_url_backwards(path, [])

    if isinstance(item, Page):
        return send_page(item, request)

    if isinstance(item, FileStore):
        return send_file(item, request)

    raise Http404


def get_tree_data(request):
    """Returns the structure of the file hierarchy in JSON format.

    Args:
      request: The Django request object

    Returns:
      A Django HttpResponse object containing the file data.

    """

    def get_node_data(page):
        """A recursive function to output individual nodes of the tree."""
        page_id = str(page.key().id())
        data = {'title': page.title,
                'path': page.path,
                'id': page_id,
                'edit_url': urlresolvers.reverse(
                    'core.views.admin.edit_page', args=[page_id]),
                'child_url': urlresolvers.reverse(
                    'core.views.admin.new_page', args=[page_id]),
                'delete_url': urlresolvers.reverse(
                    'core.views.admin.delete_page', args=[page_id])}
        children = []
        for child in page.page_children:
            max_version = Page.all().filter('name =', child.name).filter('parent_page =', child.parent_page).order('-version').get().version
            if child.acl.user_can_read(request.profile) and child.name != "New Page" and child.version == max_version:
                children.append(get_node_data(child))
        if children:
            data['children'] = children
        return data

    data = {'identifier': 'id', 'label': 'title',
            'items': [get_node_data(Page.get_root())]}
    import logging
    logging.debug('Tree node data %s', data)
    return http.HttpResponse(simplejson.dumps(data))


def page_list(request):
    """List all pages."""
    return utility.respond(request, 'sitemap')

def get_sidebar(request):
    return utility.respond(request, 'themes/frames/sidebar')

def get_root(request, to_page=None,version_num=None):
    import configuration
    logging.debug('217 version_num = %s',version_num)
    if configuration.SYSTEM_THEME_NAME == 'frames':
        if to_page:
            return utility.respond(request, 'themes/frames/base',
                    {'to_page':to_page}) 
        return utility.respond(request, 'themes/frames/base',{})
    if version_num:
        logging.debug('224 version_num = %s',version_num)
        return get_url(request, "/",version_num)
    return get_url(request, "/")

def frame_root(request): 
    page = Page.get_root()
    files = page.attached_files()
    is_editor = page.user_can_write(request.profile)
    return utility.respond(request, 'themes/frames/page', 
            {'page': page, 'files': files, 'is_editor': is_editor})
