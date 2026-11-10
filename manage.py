#!/usr/bin/env python
"""Run the demo site that hosts My Insights. Pick the environment with DJANGO_ENV."""

import os
import sys


def main():
    env = os.environ.get("DJANGO_ENV", "development")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", f"demo.settings.{env}")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
