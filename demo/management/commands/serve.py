"""Start the demo with Waitress: ONE process, many threads (My Insights keeps each user's data
in this process's memory, so never run several processes).

    uv run python manage.py serve                  # http://127.0.0.1:8000
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Start the demo site with the Waitress web server (one process, many threads)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1", help="Address to listen on.")
        parser.add_argument("--port", type=int, default=8000, help="Port (default 8000).")
        parser.add_argument("--threads", type=int, default=8, help="Requests at once (default 8).")

    def handle(self, *args, **options):
        from waitress import serve

        from demo.wsgi import application

        logger.info(
            "Starting My Insights demo (%s) on http://%s:%s with %s threads",
            settings.DJANGO_ENV,
            options["host"],
            options["port"],
            options["threads"],
        )
        self.stdout.write(f"My Insights demo on http://{options['host']}:{options['port']}")
        serve(
            application,
            host=options["host"],
            port=options["port"],
            threads=options["threads"],
            ident="My Insights",
        )
