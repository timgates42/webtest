# (c) 2005 Ian Bicking and contributors; written for Paste
# (http://pythonpaste.org)
# Licensed under the MIT license:
# http://www.opensource.org/licenses/mit-license.php
"""
Routines for testing WSGI applications.

Most interesting is TestApp
"""
from __future__ import unicode_literals

import cgi
import fnmatch
import mimetypes
import os
import random
import re
import warnings

from six import StringIO
from six import BytesIO
from six import string_types
from six import binary_type
from six import text_type
from six.moves import http_cookiejar

from webtest.compat import urlparse
from webtest.compat import urlencode
from webtest.compat import to_bytes
from webtest.compat import PY3
from webtest.response import TestResponse
from webtest import forms
from webtest import lint
from webtest import utils


import webob


__all__ = ['TestApp', 'TestRequest']


class RequestCookieAdapter(object):
    """
    this class merely provides the methods required for a
    cookielib.CookieJar to work on a webob.Request

    potential for yak shaving...very high
    """
    def __init__(self, request):
        self._request = request

    def is_unverifiable(self):
        return True  # sure? Why not?

    @property
    def unverifiable(self):  # NOQA
        # This is undocumented method that Python 3 cookielib uses
        return True

    def get_full_url(self):
        return self._request.url

    def get_origin_req_host(self):
        return self._request.host

    def add_unredirected_header(self, key, header):
        self._request.headers[key] = header

    def has_header(self, key):
        return key in self._request.headers


class ResponseCookieAdapter(object):
    """
    cookielib.CookieJar to work on a webob.Response
    """
    def __init__(self, response):
        self._response = response

    def info(self):
        return self

    def getheaders(self, header):
        return self._response.headers.getall(header)

    def get_all(self, headers, default):  # NOQA
        # This is undocumented method that Python 3 cookielib uses
        return self._response.headers.getall(headers)


class AppError(Exception):

    def __init__(self, message, *args):
        if isinstance(message, binary_type):
            message = message.decode('utf8')
        str_args = ()
        for arg in args:
            if isinstance(arg, webob.Response):
                body = arg.body
                if isinstance(body, binary_type):
                    if arg.charset:
                        arg = body.decode(arg.charset)
                    else:
                        arg = repr(body)
            elif isinstance(arg, binary_type):
                try:
                    arg = arg.decode('utf8')
                except UnicodeDecodeError:
                    arg = repr(arg)
            str_args += (arg,)
        message = message % str_args
        Exception.__init__(self, message)


class TestRequest(webob.Request):
    ResponseClass = TestResponse


class TestApp(object):
    """
    Wraps a WSGI application in a more convenient interface for
    testing.

    ``app`` may be an application, or a Paste Deploy app
    URI, like ``'config:filename.ini#test'``.

    ``extra_environ`` is a dictionary of values that should go
    into the environment for each request.  These can provide a
    communication channel with the application.

    ``relative_to`` is a directory, and filenames used for file
    uploads are calculated relative to this.  Also ``config:``
    URIs that aren't absolute.

    ``cookiejar`` is a `cookielib.CookieJar` instance that keeps cookies
    across requets. See official Python documentation for the API.

    ``cookies`` is a convenient shortcut for a dict of all cookies in
    ``cookiejar``.

    """

    RequestClass = TestRequest

    def __init__(self, app, extra_environ=None, relative_to=None,
                 use_unicode=True):
        if isinstance(app, string_types):
            from paste.deploy import loadapp
            # @@: Should pick up relative_to from calling module's
            # __file__
            app = loadapp(app, relative_to=relative_to)
        self.app = app
        self.relative_to = relative_to
        if extra_environ is None:
            extra_environ = {}
        self.extra_environ = extra_environ
        self.use_unicode = use_unicode
        self.cookiejar = http_cookiejar.CookieJar()

    @property
    def cookies(self):
        return dict([(cookie.name, cookie) for cookie in self.cookiejar])

    def reset(self):
        """
        Resets the state of the application; currently just clears
        saved cookies.
        """
        self.cookiejar.clear()

    def _make_environ(self, extra_environ=None):
        environ = self.extra_environ.copy()
        environ['paste.throw_errors'] = True
        if extra_environ:
            environ.update(extra_environ)
        return environ

    def _remove_fragment(self, url):
        scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        return urlparse.urlunsplit((scheme, netloc, path, query, ""))

    def get(self, url, params=None, headers=None, extra_environ=None,
            status=None, expect_errors=False):
        """
        Get the given url (well, actually a path like
        ``'/page.html'``).

        ``params``:
            A query string, or a dictionary that will be encoded
            into a query string.  You may also include a query
            string on the ``url``.

        ``headers``:
            A dictionary of extra headers to send.

        ``extra_environ``:
            A dictionary of environmental variables that should
            be added to the request.

        ``status``:
            The integer status code you expect (if not 200 or 3xx).
            If you expect a 404 response, for instance, you must give
            ``status=404`` or it will be an error.  You can also give
            a wildcard, like ``'3*'`` or ``'*'``.

        ``expect_errors``:
            If this is not true, then if anything is written to
            ``wsgi.errors`` it will be an error.  If it is true, then
            non-200/3xx responses are also okay.

        Returns a :class:`webtest.TestResponse` object.
        """
        environ = self._make_environ(extra_environ)
        url = str(url)
        url = self._remove_fragment(url)
        if params:
            if not isinstance(params, string_types):
                params = urlencode(params, doseq=True)
            if str('?') in url:
                url += str('&')
            else:
                url += str('?')
            url += params
        if str('?') in url:
            url, environ['QUERY_STRING'] = url.split(str('?'), 1)
        else:
            environ['QUERY_STRING'] = str('')
        req = self.RequestClass.blank(url, environ)
        if headers:
            req.headers.update(headers)
        return self.do_request(req, status=status,
                               expect_errors=expect_errors)

    def _gen_request(self, method, url, params=utils.NoDefault, headers=None,
                     extra_environ=None, status=None, upload_files=None,
                     expect_errors=False, content_type=None):
        """
        Do a generic request.
        """

        if method == 'DELETE' and params is not utils.NoDefault:
            warnings.warn(('You are not supposed to send a body in a '
                           'DELETE request. Most web servers will ignore it'),
                           lint.WSGIWarning)

        environ = self._make_environ(extra_environ)

        inline_uploads = []

        # this supports OrderedDict
        if isinstance(params, dict) or hasattr(params, 'items'):
            params = list(params.items())

        if isinstance(params, (list, tuple)):
            inline_uploads = [v for (k, v) in params
                              if isinstance(v, (forms.File, forms.Upload))]

        if len(inline_uploads) > 0:
            content_type, params = self.encode_multipart(
                params, upload_files or ())
            environ['CONTENT_TYPE'] = content_type
        else:
            params = utils.encode_params(params, content_type)
            if upload_files or \
                (content_type and
                 to_bytes(content_type).startswith(b'multipart')):
                params = cgi.parse_qsl(params, keep_blank_values=True)
                content_type, params = self.encode_multipart(
                    params, upload_files or ())
                environ['CONTENT_TYPE'] = content_type
            elif params:
                environ.setdefault('CONTENT_TYPE',
                                   str('application/x-www-form-urlencoded'))

        if content_type is not None:
            environ['CONTENT_TYPE'] = content_type
        environ['REQUEST_METHOD'] = str(method)
        url = str(url)
        url = self._remove_fragment(url)
        req = self.RequestClass.blank(url, environ)
        if isinstance(params, text_type):
            params = params.encode(req.charset or 'utf8')
        req.environ['wsgi.input'] = BytesIO(params)
        req.content_length = len(params)
        if headers:
            req.headers.update(headers)
        return self.do_request(req, status=status,
                               expect_errors=expect_errors)

    def post(self, url, params='', headers=None, extra_environ=None,
             status=None, upload_files=None, expect_errors=False,
             content_type=None):
        """
        Do a POST request.  Very like the ``.get()`` method.
        ``params`` are put in the body of the request.

        ``upload_files`` is for file uploads.  It should be a list of
        ``[(fieldname, filename, file_content)]``.  You can also use
        just ``[(fieldname, filename)]`` and the file content will be
        read from disk.

        For post requests params could be a collections.OrderedDict with
        Upload fields included in order:

            app.post('/myurl', collections.OrderedDict([
                ('textfield1', 'value1'),
                ('uploadfield', webapp.Upload('filename.txt', 'contents'),
                ('textfield2', 'value2')])))

        Returns a ``webob.Response`` object.
        """
        return self._gen_request('POST', url, params=params, headers=headers,
                                 extra_environ=extra_environ, status=status,
                                 upload_files=upload_files,
                                 expect_errors=expect_errors,
                                 content_type=content_type,
                                 )

    def put(self, url, params='', headers=None, extra_environ=None,
            status=None, upload_files=None, expect_errors=False,
            content_type=None):
        """
        Do a PUT request.  Very like the ``.post()`` method.
        ``params`` are put in the body of the request, if params is a
        tuple, dictionary, list, or iterator it will be urlencoded and
        placed in the body as with a POST, if it is string it will not
        be encoded, but placed in the body directly.

        Returns a ``webob.Response`` object.
        """
        return self._gen_request('PUT', url, params=params, headers=headers,
                                 extra_environ=extra_environ, status=status,
                                 upload_files=upload_files,
                                 expect_errors=expect_errors,
                                 content_type=content_type,
                                 )

    def patch(self, url, params='', headers=None, extra_environ=None,
              status=None, upload_files=None, expect_errors=False,
              content_type=None):
        """
        Do a PATCH request.  Very like the ``.post()`` method.
        ``params`` are put in the body of the request, if params is a
        tuple, dictionary, list, or iterator it will be urlencoded and
        placed in the body as with a POST, if it is string it will not
        be encoded, but placed in the body directly.

        Returns a ``webob.Response`` object.
        """
        return self._gen_request('PATCH', url, params=params, headers=headers,
                                 extra_environ=extra_environ, status=status,
                                 upload_files=upload_files,
                                 expect_errors=expect_errors,
                                 content_type=content_type,
                                 )

    def delete(self, url, params='', headers=None, extra_environ=None,
               status=None, expect_errors=False, content_type=None):
        """
        Do a DELETE request.  Very like the ``.get()`` method.

        Returns a ``webob.Response`` object.
        """
        return self._gen_request('DELETE', url, params=params, headers=headers,
                                 extra_environ=extra_environ, status=status,
                                 upload_files=None,
                                 expect_errors=expect_errors,
                                 content_type=content_type,
                                 )

    def options(self, url, headers=None, extra_environ=None,
                status=None, expect_errors=False):
        """
        Do a OPTIONS request.  Very like the ``.get()`` method.

        Returns a ``webob.Response`` object.
        """
        return self._gen_request('OPTIONS', url, headers=headers,
                                 extra_environ=extra_environ, status=status,
                                 upload_files=None,
                                 expect_errors=expect_errors,
                                 )

    def head(self, url, headers=None, extra_environ=None,
             status=None, expect_errors=False):
        """
        Do a HEAD request.  Very like the ``.get()`` method.

        Returns a ``webob.Response`` object.
        """
        return self._gen_request('HEAD', url, headers=headers,
                                 extra_environ=extra_environ, status=status,
                                 upload_files=None,
                                 expect_errors=expect_errors,
                                 )

    post_json = utils.json_method('POST')
    put_json = utils.json_method('PUT')
    patch_json = utils.json_method('PATCH')
    delete_json = utils.json_method('DELETE')

    def encode_multipart(self, params, files):
        """
        Encodes a set of parameters (typically a name/value list) and
        a set of files (a list of (name, filename, file_body)) into a
        typical POST body, returning the (content_type, body).
        """
        boundary = to_bytes(str(random.random()))[2:]
        boundary = b'----------a_BoUnDaRy' + boundary + b'$'
        lines = []

        def _append_file(file_info):
            key, filename, value = self._get_file_info(file_info)
            if isinstance(filename, text_type):
                try:
                    key = key.encode('utf8')
                except:
                    raise  # file names must be ascii
            if isinstance(filename, text_type):
                fcontent = mimetypes.guess_type(filename)[0]
                try:
                    filename = filename.encode('utf8')
                except:
                    raise  # file names must be ascii
            else:
                fcontent = mimetypes.guess_type(filename.decode('ascii'))[0]
            if isinstance(value, text_type):
                try:
                    value = value.encode('ascii')
                except:
                    raise TypeError(
                            'You are trying to upload some non ascii content.'
                            'Please encode it first')
            fcontent = to_bytes(fcontent)
            fcontent = fcontent or b'application/octet-stream'
            lines.extend([
                b'--' + boundary,
                b'Content-Disposition: form-data; ' + \
                b'name="' + key + b'"; filename="' + filename + b'"',
                b'Content-Type: ' + fcontent, b'', value])

        for key, value in params:
            if isinstance(key, text_type):
                try:
                    key = key.encode('ascii')
                except:
                    raise  # field name are always ascii
            if isinstance(value, forms.File):
                if value.value:
                    _append_file([key] + list(value.value))
            elif isinstance(value, forms.Upload):
                file_info = [key, value.filename]
                if value.content is not None:
                    file_info.append(value.content)
                _append_file(file_info)
            else:
                if isinstance(value, text_type):
                    value = value.encode('utf8')
                lines.extend([
                    b'--' + boundary,
                    b'Content-Disposition: form-data; name="' + key + b'"',
                    b'', value])

        for file_info in files:
            _append_file(file_info)

        lines.extend([b'--' + boundary + b'--', b''])
        body = b'\r\n'.join(lines)
        boundary = boundary.decode('ascii')
        content_type = 'multipart/form-data; boundary=%s' % boundary
        return content_type, body

    def _get_file_info(self, file_info):
        if len(file_info) == 2:
            # It only has a filename
            filename = file_info[1]
            if self.relative_to:
                filename = os.path.join(self.relative_to, filename)
            f = open(filename, 'rb')
            content = f.read()
            if PY3 and isinstance(content, text_type):
                # we want bytes
                content = content.encode(f.encoding)
            f.close()
            return (file_info[0], filename, content)
        elif len(file_info) == 3:
            content = file_info[2]
            if not isinstance(content, binary_type):
                raise ValueError('File content must be %s not %s'
                                 % (binary_type, type(content)))
            return file_info
        else:
            raise ValueError(
                "upload_files need to be a list of tuples of (fieldname, "
                "filename, filecontent) or (fieldname, filename); "
                "you gave: %r"
                % repr(file_info)[:100])

    def request(self, url_or_req, status=None, expect_errors=False,
                **req_params):
        """
        Creates and executes a request.  You may either pass in an
        instantiated :class:`TestRequest` object, or you may pass in a
        URL and keyword arguments to be passed to
        :meth:`TestRequest.blank`.

        You can use this to run a request without the intermediary
        functioning of :meth:`TestApp.get` etc.  For instance, to
        test a WebDAV method::

            resp = app.request('/new-col', method='MKCOL')

        Note that the request won't have a body unless you specify it,
        like::

            resp = app.request('/test.txt', method='PUT', body='test')

        You can use ``POST={args}`` to set the request body to the
        serialized arguments, and simultaneously set the request
        method to ``POST``
        """
        if isinstance(url_or_req, text_type):
            url_or_req = str(url_or_req)
        for (k, v) in req_params.items():
            if isinstance(v, text_type):
                req_params[k] = str(v)
        if isinstance(url_or_req, string_types):
            req = self.RequestClass.blank(url_or_req, **req_params)
        else:
            req = url_or_req.copy()
            for name, value in req_params.items():
                setattr(req, name, value)
            if req.content_length == -1:
                req.content_length = len(req.body)
        req.environ['paste.throw_errors'] = True
        for name, value in self.extra_environ.items():
            req.environ.setdefault(name, value)
        return self.do_request(req,
                               status=status,
                               expect_errors=expect_errors,
                               )

    def do_request(self, req, status, expect_errors):
        """
        Executes the given request (``req``), with the expected
        ``status``.  Generally ``.get()`` and ``.post()`` are used
        instead.

        To use this::

            resp = app.do_request(webtest.TestRequest.blank(
                'url', ...args...))

        Note you can pass any keyword arguments to
        ``TestRequest.blank()``, which will be set on the request.
        These can be arguments like ``content_type``, ``accept``, etc.
        """

        errors = StringIO()
        req.environ['wsgi.errors'] = errors
        script_name = req.environ.get('SCRIPT_NAME', '')
        if script_name and req.path_info.startswith(script_name):
            req.path_info = req.path_info[len(script_name):]

        req.environ['paste.testing'] = True
        req.environ['paste.testing_variables'] = {}

        # verify wsgi compatibility
        app = lint.middleware(self.app)

        ## FIXME: should it be an option to not catch exc_info?
        res = req.get_response(app, catch_exc_info=True)
        # set a few handy attributes
        res._use_unicode = self.use_unicode
        res.request = req
        res.app = app
        res.test_app = self

        # We do this to make sure the app_iter is exausted:
        try:
            res.body
        except TypeError:  # pragma: no cover
            pass
        res.errors = errors.getvalue()

        for name, value in req.environ['paste.testing_variables'].items():
            if hasattr(res, name):
                raise ValueError(
                    "paste.testing_variables contains the variable %r, but "
                    "the response object already has an attribute by that "
                    "name" % name)
            setattr(res, name, value)
        if not expect_errors:
            self._check_status(status, res)
            self._check_errors(res)

        # merge cookies back in
        self.cookiejar.extract_cookies(ResponseCookieAdapter(res),
                                        RequestCookieAdapter(req))

        return res

    def _check_status(self, status, res):
        if status == '*':
            return
        res_status = res.status
        if (isinstance(status, string_types)
            and '*' in status):
            if re.match(fnmatch.translate(status), res_status, re.I):
                return
        if isinstance(status, (list, tuple)):
            if res.status_int not in status:
                raise AppError(
                    "Bad response: %s (not one of %s for %s)\n%s",
                    res_status, ', '.join(map(str, status)),
                    res.request.url, res)
            return
        if status is None:
            if res.status_int >= 200 and res.status_int < 400:
                return
            raise AppError(
                "Bad response: %s (not 200 OK or 3xx redirect for %s)\n%s",
                res_status, res.request.url,
                res)
        if status != res.status_int:
            raise AppError(
                "Bad response: %s (not %s)", res_status, status)

    def _check_errors(self, res):
        errors = res.errors
        if errors:
            raise AppError(
                "Application had errors logged:\n%s", errors)
