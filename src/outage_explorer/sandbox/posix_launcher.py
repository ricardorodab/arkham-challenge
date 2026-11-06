"""Apply POSIX child resource limits before execing trusted compiler or Node.

The launcher does not parse SQL or establish a filesystem/network sandbox. Node
engine capabilities are restricted separately by WASM imports and permission
flags. RLIMIT_DATA bounds data allocations, including writable private mappings
on Linux, not total RSS or Windows-equivalent process memory. Linux engine mode
budgets the pinned runtime's code reservation separately from execution data;
other modes retain the smaller data limit.
"""

import os
import resource
import sys


def main() -> None:
    """Install hard allocation/CPU/output/descriptor limits and replace this process.

    Default/compiler mode permits 512 MiB of data mappings. The trusted parent
    selects ``--engine`` only for Node. Linux permits 1 GiB to budget the pinned
    Linux x64 Node runtime's 512 MiB JavaScript CodeRange reservation separately
    from 512 MiB of execution mappings. V8 makes the unused reservation writable,
    so Linux counts it against the data limit even at low resident memory.
    CodeRange sizes vary by architecture. Other POSIX engines retain 512 MiB;
    the Linux mapping allowance is not a 1 GiB RSS guarantee.

    Both modes retain six CPU seconds, no core/file output and 64 descriptors.
    Exec the absolute program with the scrubbed environment; OS failures
    propagate and no child fallback is launched.
    """
    command = sys.argv[1:]
    engine = bool(command) and command[0] == "--engine"
    if engine:
        command = command[1:]
    limit = (1024 if engine and sys.platform == "linux" else 512) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.execve(command[0], command, dict(os.environ))


if __name__ == "__main__":
    main()
