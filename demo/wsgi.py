import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", f"demo.settings.{os.environ.get('DJANGO_ENV', 'development')}"
)
application = get_wsgi_application()
