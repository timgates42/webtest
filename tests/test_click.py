#coding: utf-8
from __future__ import unicode_literals
import webtest
from webob import Request
from webtest.compat import PY3
from tests.compat import unittest


def links_app(environ, start_response):
    req = Request(environ)
    status = "200 OK"
    responses = {
       '/': """
            <html>
                <head><title>page with links</title></head>
                <body>
                    <a href="/foo/">Foo</a>
                    <a href='bar'>Bar</a>
                    <a href='baz/' id='id_baz'>Baz</a>
                    <a href='#' id='fake_baz'>Baz</a>
                    <a href='javascript:alert("123")' id='js_baz'>Baz</a>
                    <script>
                        var link = "<a href='/boo/'>Boo</a>";
                    </script>
                    <a href='/spam/'>Click me!</a>
                    <a href='/egg/'>Click me!</a>
                    <button id="button1" onclick="location.href='/foo/'">Button</button>
                </body>
            </html>
            """,

       '/foo/': ('<html><body>This is foo. <a href="bar">Bar</a> '
                 '</body></html>'),
       '/foo/bar': '<html><body>This is foobar.</body></html>',
       '/bar': '<html><body>This is bar.</body></html>',
       '/baz/': '<html><body>This is baz.</body></html>',
       '/spam/': '<html><body>This is spam.</body></html>',
       '/egg/': '<html><body>Just eggs.</body></html>',

       '/utf8/': """
            <html>
                <head><title>Тестовая страница</title></head>
                <body>
                    <a href='/foo/'>Менделеев</a>
                    <a href='/baz/' title='Поэт'>Пушкин</a>
                    <img src='/egg/' title='Поэт'>
                    <script>
                        var link = "<a href='/boo/'>Злодейская ссылка</a>";
                    </script>
                </body>
            </html>
            """,
    }

    utf8_paths = ['/utf8/']
    body = responses[req.path_info]
    body = body.encode('utf8')
    headers = [
        ('Content-Type', str('text/html')),
        ('Content-Length', str(len(body)))
    ]
    if req.path_info in utf8_paths:
        headers[0] = ('Content-Type', str('text/html; charset=utf-8'))

    start_response(str(status), headers)
    return [body]


class TestClick(unittest.TestCase):

    def test_click(self):
        app = webtest.TestApp(links_app)
        self.assertIn('This is foo.', app.get('/').click('Foo'))
        self.assertIn('This is foobar.',
               app.get('/').click('Foo').click('Bar'))
        self.assertIn('This is bar.', app.get('/').click('Bar'))
        # should skip non-clickable links
        self.assertIn('This is baz.',
               app.get('/').click('Baz'))
        self.assertIn('This is baz.', app.get('/').click(linkid='id_baz'))
        self.assertIn('This is baz.', app.get('/').click(href='baz/'))
        self.assertIn('This is baz.',
               app.get('/').click(anchor="<a href='baz/' id='id_baz'>Baz</a>"))
        self.assertIn('This is spam.',
               app.get('/').click('Click me!', index=0))
        self.assertIn('Just eggs.',
               app.get('/').click('Click me!', index=1))

        def multiple_links():
            app.get('/').click('Click me!')
        self.assertRaises(IndexError, multiple_links)

        def invalid_index():
            app.get('/').click('Click me!', index=2)
        self.assertRaises(IndexError, invalid_index)

        def no_links_found():
            app.get('/').click('Ham')
        self.assertRaises(IndexError, no_links_found)

        def tag_inside_script():
            app.get('/').click('Boo')
        self.assertRaises(IndexError, tag_inside_script)

    def test_click_utf8(self):
        app = webtest.TestApp(links_app, use_unicode=False)
        resp = app.get('/utf8/')
        self.assertEqual(resp.charset, 'utf-8')
        if not PY3:
            # No need to deal with that in Py3
            self.assertIn("Тестовая страница".encode('utf8'), resp)
            self.assertIn("Тестовая страница", resp, resp)
            target = 'Менделеев'.encode('utf8')
            self.assertIn('This is foo.', resp.click(target, verbose=True))

            # should skip the img tag
            anchor = ".*title='Поэт'.*"
            anchor_re = anchor.encode('utf8')
            self.assertIn('This is baz.', resp.click(anchor=anchor_re))

    def test_click_u(self):
        app = webtest.TestApp(links_app)
        resp = app.get('/utf8/')

        self.assertIn("Тестовая страница", resp)
        self.assertIn('This is foo.', resp.click('Менделеев'))
        self.assertIn('This is baz.', resp.click(anchor=".*title='Поэт'.*"))

    def test_clickbutton(self):
        app = webtest.TestApp(links_app)
        self.assertIn(
            'This is foo.',
            app.get('/').clickbutton(buttonid='button1')
        )
