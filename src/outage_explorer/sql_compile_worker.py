"""Implement the isolated compiler child's bounded JSON stdin/stdout protocol.

This trusted Python worker parses policy input, discovers all named references,
and regenerates allowed SQL. It never opens model data or executes SQL. The
parent authorizes returned references before handling a shape error. Resource
isolation is supplied by the parent/launcher; this is not a filesystem jail.
"""

from __future__ import annotations

import json
import logging
import sys

# Unsupported SQL must not become a parser warning containing its source text.
logging.disable(logging.CRITICAL)


def main() -> None:
    """Consume one bounded compile request and emit references plus query or code.

    Discover references before supported-shape validation so forbidden nested names
    remain visible to the parent authorizer. Reject malformed protocol without
    returning SQL/parser diagnostics. Oversized output becomes a fixed error frame;
    stdout is flushed once and parser logging remains disabled.
    """
    from .sql_compiler import ERROR_MESSAGES, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES
    from .sql_policy import SQLPolicyError, referenced_dataset_ids, validate_sql

    response = {"dataset_ids": [], "query": None, "error": {"code": "invalid_sql"}}
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            response["error"] = {"code": "sql_too_large"}
        else:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"sql", "dataset_id", "max_rows"}:
                raise ValueError
            try:
                references = referenced_dataset_ids(payload["sql"])
                response["dataset_ids"] = list(references)
                query = validate_sql(
                    payload["sql"], payload["dataset_id"], max_rows=payload["max_rows"]
                )
            except SQLPolicyError as error:
                response["error"] = {
                    "code": error.code if error.code in ERROR_MESSAGES else "invalid_sql"
                }
            else:
                response["query"] = {
                    "sql": query.sql,
                    "dataset_ids": list(query.dataset_ids),
                    "row_limit": query.row_limit,
                }
                response["error"] = None
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        pass
    encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = b'{"dataset_ids":[],"query":null,"error":{"code":"sql_too_large"}}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
