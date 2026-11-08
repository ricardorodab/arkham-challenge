"""Compile a bounded, single-dataset SELECT language without analytical I/O.

This SQLGlot AST allowlist is neither the persona authorizer nor the execution
sandbox. Callers authorize every ``referenced_dataset_ids`` result, including
unknown names and names inside unsupported shapes, before opening model data.
Only regenerated ``ValidatedQuery.sql`` may reach the separate WASM worker.

Deterministic key ordering supports immutable-model pagination. The outer cap
fetches one sentinel row beyond ``row_limit``; an explicit inner SQL LIMIT keeps
its ordinary semantics. Bounds cover source text, tokens, tree shape and output
SQL, not the execution cost of a permitted aggregate or sort.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from outage_explorer.analytics.model_contracts import DATASETS

MAX_SQL_BYTES = 16 * 1024
MAX_TOKENS = 2048
MAX_AST_NODES = 512
MAX_DEPTH = 32
MAX_LITERAL_BYTES = 1024
MAX_IDENTIFIER_BYTES = 128
MAX_ROWS = 1000
MAX_OFFSET = 100_000


class SQLPolicyError(ValueError):
    """Represent a static SQL-language rejection with a service-facing error code."""

    def __init__(self, message: str, *, code: str = "invalid_sql") -> None:
        """Attach the bounded policy code while retaining the safe public message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedQuery:
    """Hold regenerated SQL, discovered dataset names and the client result cap.

    The generated statement may retrieve one additional private sentinel row. This
    object records language validation, not caller authorization or engine safety.
    """

    sql: str
    dataset_ids: tuple[str, ...]
    row_limit: int


def _unsupported() -> SQLPolicyError:
    """Construct the common static error for syntax outside the AST allowlist."""
    return SQLPolicyError(
        "SQL uses a feature outside the supported single-dataset SELECT subset.",
        code="unsupported_sql",
    )


def _complex() -> SQLPolicyError:
    """Construct the common static error for exceeded compiler complexity bounds."""
    return SQLPolicyError("SQL exceeds the supported complexity limits.", code="sql_too_complex")


def _parse_bounded(sql: str) -> exp.Expression:
    """Parse one DuckDB-dialect statement after text/token/nesting preflight.

    Bound UTF-8 bytes, token/literal lengths and nesting before recursive parsing;
    then bound AST nodes/depth with an explicit stack. Exclude command fallback to
    avoid logging source SQL. A sole trailing semicolon/comment is accepted; extra
    statements and malformed/over-complex input raise safe policy errors.
    """
    if not isinstance(sql, str):
        raise SQLPolicyError("SQL must be a nonempty string.")
    try:
        size = len(sql.encode("utf-8"))
    except UnicodeError:
        raise SQLPolicyError("SQL contains invalid text.") from None
    if size > MAX_SQL_BYTES:
        raise SQLPolicyError("SQL exceeds the 16 KiB input limit.", code="sql_too_large")
    try:
        tokens = sqlglot.tokenize(sql, read="duckdb")
    except (SqlglotError, ValueError, RecursionError):
        raise SQLPolicyError("SQL could not be parsed.") from None
    if not tokens:
        raise SQLPolicyError("SQL must be a nonempty string.")
    if len(tokens) > MAX_TOKENS:
        raise _complex()
    # Bound nesting before the recursive parser sees it. Token kinds distinguish
    # parentheses in strings/comments from actual SQL syntax.
    depth = 0
    for token in tokens:
        kind = token.token_type.name
        if kind in {"L_PAREN", "L_BRACKET", "L_BRACE"}:
            depth += 1
            if depth > MAX_DEPTH:
                raise _complex()
        elif kind in {"R_PAREN", "R_BRACKET", "R_BRACE"}:
            depth -= 1
        if kind == "STRING" and len(token.text.encode("utf-8")) > MAX_LITERAL_BYTES:
            raise _complex()
        if kind == "NUMBER" and len(token.text) > 64:
            raise _complex()
    semicolons = [i for i, token in enumerate(tokens) if token.token_type.name == "SEMICOLON"]
    if semicolons and semicolons != [len(tokens) - 1]:
        raise SQLPolicyError(
            "Exactly one SELECT statement is supported.", code="multiple_statements"
        )
    # Avoid SQLGlot's command fallback, which can log original unsupported SQL.
    if tokens[0].token_type.name not in {"SELECT", "WITH"}:
        raise _unsupported()
    try:
        parsed = sqlglot.parse(sql, read="duckdb", error_level=ErrorLevel.RAISE)
    except (SqlglotError, ValueError, RecursionError):
        raise SQLPolicyError("SQL could not be parsed.") from None
    # SQLGlot represents a comment after the sole trailing semicolon as a
    # comment-only Semicolon node. The token check above excludes real extras.
    parsed = [node for node in parsed if node is not None and not isinstance(node, exp.Semicolon)]
    if len(parsed) != 1:
        raise SQLPolicyError(
            "Exactly one SELECT statement is supported.", code="multiple_statements"
        )
    tree = parsed[0]
    count = 0
    stack = [(tree, 1)]
    while stack:
        node, level = stack.pop()
        count += 1
        if count > MAX_AST_NODES or level > MAX_DEPTH:
            raise _complex()
        stack.extend((child, level + 1) for child in node.iter_expressions())
    return tree


def _references(tree: exp.Expression) -> tuple[str, ...]:
    # Include references in forbidden nested queries/CTEs before checking shape.
    # A table function has no named table identifier; it is rejected separately.
    """Return sorted case-folded named tables from every parsed scope.

    Traverse unsupported nested/CTE/join shapes too. Table functions have no named
    identifier here and are rejected separately by shape/node validation.
    """
    return tuple(
        sorted(
            {
                table.name.casefold()
                for table in tree.find_all(exp.Table)
                if isinstance(table.this, exp.Identifier)
            }
        )
    )


def referenced_dataset_ids(sql: str) -> tuple[str, ...]:
    """Discover all bounded named references without opening catalog or data.

    Unknown names and unsupported nested scopes are returned for caller denial;
    this discovery result never authorizes them. Parse/complexity failures raise
    ``SQLPolicyError`` before a complete reference result can be produced.
    """
    return _references(_parse_bounded(sql))


_BINARY = {
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.IntDiv,
    exp.Mod,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.And,
    exp.Or,
    exp.Is,
    exp.Like,
    exp.ILike,
}
_UNARY = {exp.Paren, exp.Neg, exp.Not, exp.Where, exp.Having, exp.Abs}
_ARGS: dict[type[exp.Expression], set[str]] = {
    exp.Select: {"expressions", "from_", "where", "group", "having", "order", "limit"},
    exp.From: {"this"},
    exp.Table: {"this", "alias"},
    exp.TableAlias: {"this"},
    exp.Identifier: {"this", "quoted"},
    exp.Column: {"this", "table"},
    exp.Star: set(),
    exp.Alias: {"this", "alias"},
    exp.Literal: {"this", "is_string"},
    exp.Boolean: {"this"},
    exp.Null: set(),
    exp.Group: {"expressions"},
    exp.Order: {"expressions"},
    exp.Ordered: {"this", "desc", "nulls_first"},
    exp.Limit: {"expression"},
    exp.Between: {"this", "low", "high"},
    exp.In: {"this", "expressions"},
    exp.Count: {"this", "big_int"},
    exp.Sum: {"this"},
    exp.Avg: {"this"},
    exp.Min: {"this"},
    exp.Max: {"this"},
    exp.Distinct: {"expressions"},
    exp.Round: {"this", "decimals"},
    exp.Coalesce: {"this", "expressions"},
    exp.Cast: {"this", "to"},
    exp.DataType: {"this", "expressions", "nested"},
    exp.DataTypeParam: {"this"},
}
_ARGS.update({kind: {"this", "expression"} for kind in _BINARY})
_ARGS.update({kind: {"this"} for kind in _UNARY})
_ARGS[exp.Div] |= {"typed", "safe"}
_AGGREGATES = {exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max}
_SCALAR_TYPES = {
    "BOOLEAN",
    "DATE",
    "TINYINT",
    "SMALLINT",
    "INT",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINT",
    "UBIGINT",
    "UHUGEINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
    "CHAR",
    "VARCHAR",
    "TEXT",
}


def _integer(node: exp.Expression | None) -> int:
    """Read a signed integer literal used by bounded SQL syntax parameters.

    Only a literal with an optional unary minus is accepted; expressions and other
    literal types raise the common unsupported-shape error.
    """
    sign = 1
    if isinstance(node, exp.Neg):
        sign, node = -1, node.this
    if not isinstance(node, exp.Literal) or not node.is_int:
        raise _unsupported()
    return sign * int(node.this)


def _literal(node: exp.Literal) -> None:
    """Validate one string or finite numeric literal against policy envelopes.

    Strings are byte-bounded. Numeric spelling, significant digits and exponents
    are bounded using Decimal without converting through binary floating point.
    """
    if node.is_string:
        if len(node.this.encode("utf-8")) > MAX_LITERAL_BYTES:
            raise _complex()
        return
    if len(node.this) > 64:
        raise _complex()
    try:
        value = Decimal(node.this)
    except InvalidOperation:
        raise SQLPolicyError("Numeric literals must be finite and bounded.") from None
    if (
        not value.is_finite()
        or len(value.as_tuple().digits) > 38
        or abs(value.as_tuple().exponent) > 38
        or abs(value.adjusted()) > 38
    ):
        raise _complex()


def _data_type(node: exp.DataType) -> None:
    """Permit only allowlisted non-nested scalar CAST targets and parameters.

    Validate Decimal precision/scale, bounded character lengths and absence of
    parameters on other scalar types. Unsupported shapes raise policy errors.
    """
    name = getattr(node.this, "value", "")
    parameters = node.expressions
    if name not in _SCALAR_TYPES or node.args.get("nested"):
        raise _unsupported()
    values = [_integer(parameter.this) for parameter in parameters]
    if name == "DECIMAL":
        if len(values) not in (0, 1, 2):
            raise _unsupported()
        if values and not 1 <= values[0] <= 38:
            raise _unsupported()
        if len(values) == 2 and not 0 <= values[1] <= values[0]:
            raise _unsupported()
    elif name in {"VARCHAR", "CHAR", "TEXT"}:
        if len(values) > 1 or (values and not 1 <= values[0] <= MAX_LITERAL_BYTES):
            raise _unsupported()
    elif parameters:
        raise _unsupported()


def _validate_nodes(tree: exp.Select) -> None:
    """Validate each AST node and argument, normalizing identifiers in place.

    Reject unknown nodes and unsupported arguments even on otherwise allowed node
    classes. Bound identifiers/literals/types, star placement, aggregate DISTINCT,
    ROUND precision and COALESCE arity; scalar/aggregate permission is explicit.
    """
    for node in tree.walk():
        allowed = _ARGS.get(type(node))
        if allowed is None:
            raise _unsupported()
        if any(
            key not in allowed and value is not None and value is not False and value != []
            for key, value in node.args.items()
        ):
            raise _unsupported()
        if isinstance(node, exp.Identifier):
            if not node.this or len(node.this.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
                raise _complex()
            node.set("this", node.this.casefold())
            node.set("quoted", True)
        elif isinstance(node, exp.Literal):
            _literal(node)
        elif isinstance(node, exp.DataType):
            _data_type(node)
        elif isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise _unsupported()
        elif isinstance(node, exp.Star):
            if not (
                node.parent is tree
                or isinstance(node.parent, exp.Count)
                or (isinstance(node.parent, exp.Column) and node.parent.parent is tree)
            ):
                raise _unsupported()
        elif isinstance(node, exp.Distinct):
            if type(node.parent) not in _AGGREGATES or len(node.expressions) != 1:
                raise _unsupported()
        elif type(node) in _AGGREGATES and node.this is None:
            raise _unsupported()
        elif isinstance(node, exp.Round) and node.args.get("decimals") is not None:
            if not -12 <= _integer(node.args["decimals"]) <= 12:
                raise _unsupported()
        elif isinstance(node, exp.Coalesce) and len(node.expressions) > 15:
            raise _complex()


def _validate_columns(tree: exp.Select, dataset_id: str, table: exp.Table) -> None:
    """Bind columns to the selected contract and normalize their names in place.

    Permit the source's table alias and projection aliases only in unqualified
    GROUP/ORDER/HAVING contexts. Reject unknown columns and foreign qualifiers;
    this is schema checking, not a product-persona authorization decision.
    """
    columns = {name.casefold(): name for name, _ in DATASETS[dataset_id].columns}
    aliases = {
        projection.alias.casefold()
        for projection in tree.expressions
        if isinstance(projection, exp.Alias)
    }
    qualifier = table.alias_or_name.casefold()
    for column in tree.find_all(exp.Column):
        if column.table and column.table.casefold() != qualifier:
            raise SQLPolicyError(
                "Column qualifier does not match the selected source.", code="unknown_column"
            )
        if isinstance(column.this, exp.Star):
            continue
        name = column.name.casefold()
        alias_context = column.find_ancestor(exp.Order, exp.Group, exp.Having) is not None
        if name not in columns and not (not column.table and alias_context and name in aliases):
            raise SQLPolicyError("SQL references an unavailable column.", code="unknown_column")
        column.this.set("this", columns.get(name, name))


def _stable_order(tree: exp.Select, dataset_id: str, table: exp.Table) -> None:
    """Append deterministic tie breakers to the SELECT AST in place.

    Use group expressions for grouped results and full primary keys for ordinary
    rows. Scalar aggregates need no extra key. Preserve existing ordering and avoid
    adding equivalent keys, keeping page order stable on immutable model data.
    """
    group = tree.args.get("group")
    if group:
        tie_breakers = [key.copy() for key in group.expressions]
    elif any(type(node) in _AGGREGATES for node in tree.walk()):
        tie_breakers = []  # A scalar aggregate has at most one result row.
    else:
        tie_breakers = [
            exp.column(key, table=table.alias_or_name, quoted=True)
            for key in DATASETS[dataset_id].primary_key
        ]
    order = tree.args.get("order")
    ordered = list(order.expressions) if order else []
    present = {item.this.sql(dialect="duckdb") for item in ordered}
    ordered.extend(
        exp.Ordered(this=key, desc=False, nulls_first=False)
        for key in tie_breakers
        if key.sql(dialect="duckdb") not in present
    )
    if ordered:
        tree.set("order", exp.Order(expressions=ordered))


def validate_sql(
    sql: str, dataset_id: str, *, max_rows: int = MAX_ROWS, offset: int = 0
) -> ValidatedQuery:
    """Validate and regenerate a single selected-dataset SELECT with a sentinel.

    Args:
        sql: Bounded source text parsed in the DuckDB dialect.
        dataset_id: Registered logical source; no filesystem path is accepted.
        max_rows: Client result cap, independent of an explicit inner SQL LIMIT.
        offset: Trusted compiler pagination offset; SQL OFFSET itself is rejected.

    Returns:
        A ``ValidatedQuery`` wrapping the ordered inner SELECT with ``max_rows + 1``
        and the requested outer offset. The caller trims any sentinel.

    Raises:
        SQLPolicyError: Unknown/mismatched source, invalid pagination, unsupported
            shape/column/type, complexity violation or oversized generated text.

    Normalize identifiers, validate one direct table, preserve an inner LIMIT and
    add key/group tie breakers before regeneration. This function does not check a
    persona, read analytical data or bound the engine's work for aggregates/sorts.
    """
    if not isinstance(dataset_id, str) or dataset_id.casefold() not in DATASETS:
        raise SQLPolicyError("The selected dataset is unavailable.", code="unknown_dataset")
    dataset_id = dataset_id.casefold()
    if (
        type(max_rows) is not int
        or not 1 <= max_rows <= MAX_ROWS
        or type(offset) is not int
        or not 0 <= offset <= MAX_OFFSET
    ):
        raise SQLPolicyError(
            "Row limit or offset is outside the supported range.", code="invalid_pagination"
        )
    tree = _parse_bounded(sql)
    references = _references(tree)
    if any(reference != dataset_id for reference in references):
        raise SQLPolicyError(
            "SQL references a dataset other than the selected source.", code="dataset_mismatch"
        )
    if not isinstance(tree, exp.Select):
        raise _unsupported()
    if tree.args.get("offset") is not None:
        raise SQLPolicyError("Use API pagination instead of SQL OFFSET.", code="invalid_pagination")
    tables = list(tree.find_all(exp.Table))
    source = tree.args.get("from_")
    if (
        len(tables) != 1
        or source is None
        or source.this is not tables[0]
        or references != (dataset_id,)
    ):
        raise _unsupported()
    _validate_nodes(tree)
    _validate_columns(tree, dataset_id, tables[0])
    limit = tree.args.get("limit")
    if limit is not None and not 0 <= _integer(limit.expression) <= MAX_OFFSET:
        raise SQLPolicyError("SQL LIMIT is outside the supported range.", code="invalid_pagination")
    _stable_order(tree, dataset_id, tables[0])
    wrapped = exp.select("*").from_(tree.subquery("_result")).limit(max_rows + 1).offset(offset)
    try:
        generated = wrapped.sql(
            dialect="duckdb", comments=False, unsupported_level=ErrorLevel.RAISE
        )
    except (SqlglotError, ValueError, RecursionError):
        raise _unsupported() from None
    if len(generated.encode("utf-8")) > MAX_SQL_BYTES:
        raise SQLPolicyError(
            "Generated SQL exceeds the execution input limit.", code="sql_too_large"
        )
    return ValidatedQuery(generated, (dataset_id,), max_rows)
