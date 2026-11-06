"""Expose ordinary Django management commands with environment-owned configuration.

This entry point is separate from the data/model CLI. It does not load private local
credentials itself; web.local is the explicit generated-configuration wrapper for local
operators.
"""

import os
import sys

from django.core.management import execute_from_command_line


def main() -> None:
    """Select default Django settings and dispatch the command in sys.argv."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
