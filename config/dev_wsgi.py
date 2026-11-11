"""Mount the app at /mrp under runserver, the way Apache's WSGIScriptAlias does.

    WSGI_APPLICATION=config.dev_wsgi.application python manage.py runserver 8000

Not used by Apache (config/wsgi.py is).
"""
from .wsgi import application as _app

PREFIX = '/mrp'


def application(environ, start_response):
    path = environ.get('PATH_INFO', '')
    if path == PREFIX or path.startswith(PREFIX + '/'):
        environ['SCRIPT_NAME'] = PREFIX
        environ['PATH_INFO'] = path[len(PREFIX):] or '/'
    return _app(environ, start_response)
