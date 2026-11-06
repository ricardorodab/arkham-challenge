"""Apply POSIX child resource limits before execing trusted compiler or Node.

The launcher does not parse SQL or establish a filesystem/network sandbox. Node
engine capabilities are restricted separately by WASM imports and permission
flags. RLIMIT_DATA limits data allocations, not total RSS or Windows-equivalent
process memory; target-platform acceptance is required before such claims.
"""

import os
import resource
import sys


def main() -> None:
    """Install hard allocation/CPU/output/descriptor limits and replace this process.

    Set 512 MiB RLIMIT_DATA, six CPU seconds, no core/file output and 64 descriptors,
    then exec the supplied absolute program with the already-scrubbed environment.
    OS failures propagate to the supervising parent; no child fallback is launched.
    """
    limit = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.execve(sys.argv[1], sys.argv[1:], dict(os.environ))


if __name__ == "__main__":
    main()
