"""Trusted resource bootstrap; never imports or interprets submitted SQL."""

import os
import resource
import sys


def main() -> None:
    limit = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.execve(sys.argv[1], sys.argv[1:], dict(os.environ))


if __name__ == "__main__":
    main()
