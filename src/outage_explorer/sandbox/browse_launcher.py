"""Apply native-only POSIX hard limits before starting the trusted browse child."""

import os
import resource
import sys


def main() -> None:
    """Bound address space, stacks, CPU, file output and descriptors before exec."""
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_STACK, (1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    command = sys.argv[1:]
    os.execve(command[0], command, dict(os.environ))


if __name__ == "__main__":
    main()
