"""Turn driver values from either database into one exact, comparable text form (Polars).

Every value becomes either null (SQL NULL) or a string, and equality of those strings is
the comparison rule. The conversion never loses information:

* decimal / float -> exact decimal text, trailing zeros removed (1.50000 == 1.5, 1.50001 != 1.5).
                     Floats go through repr(), their shortest exact form.
* int             -> integer text; a non-integral value is shown as '<not an integer: ...>'.
* datetime        -> 'YYYY-MM-DD HH:MM:SS.ffffff' (exact), or, with datetime_precision
                     'sqlserver_datetime', rounded to SQL Server's 1/300 s steps on BOTH sides
                     and shown as 'YYYY-MM-DD HH:MM:SS.fff'. That is exactly the value SQL Server
                     stores for the same input, so it is an equivalence, not a tolerance.
* date            -> 'YYYY-MM-DD' (a datetime with a time part keeps its time, so it differs).
* status          -> the meaning from aliases.yaml (the numeric codes differ between systems).
* downcode        -> canonical spelling for predefined codes (NO_DATA <-> no_data), else unchanged.

Strict mode (default) compares strings exactly as stored. Lenient mode also trims whitespace,
treats '' as null and strips leading zeros from interface ids.

The common driver types (str, int, datetime) are converted with vectorised Polars
expressions; anything unusual falls back to an exact per-value Python conversion.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import polars as pl

from .config import Aliases, ColumnSpec

NULL_KEY = "<NULL>"
_EXACT_FMT = "%Y-%m-%d %H:%M:%S%.6f"
_SQL_FMT = "%Y-%m-%d %H:%M:%S%.3f"


# --------------------------------------------------------------------------- per-value (fallback)

def _is_null(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def canon_decimal(v) -> str | None:
    if _is_null(v):
        return None
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, float):
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        v = repr(v)
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return f"<not a number: {v!r}>"
    if d.is_nan():
        return "NaN"
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


def canon_int(v) -> str | None:
    if _is_null(v):
        return None
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return f"<not an integer: {v!r}>"
    if not d.is_finite() or d != d.to_integral_value():
        return f"<not an integer: {v!r}>"
    return str(int(d))


def canon_date(v) -> str | None:
    if _is_null(v):
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.strip())
        except ValueError:
            return f"<not a date: {v!r}>"
    if isinstance(v, datetime):
        # a SQL Server datetime used as a date must not carry a time part
        if (v.hour, v.minute, v.second, v.microsecond) != (0, 0, 0, 0):
            return v.strftime("%Y-%m-%d %H:%M:%S.%f")
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return f"<not a date: {v!r}>"


def canon_bool(v) -> str | None:
    if _is_null(v):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, Decimal)) and v in (0, 1):
        return "true" if v == 1 else "false"
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "y", "yes"):
        return "true"
    if s in ("0", "false", "f", "n", "no"):
        return "false"
    return f"<not a boolean: {v!r}>"


def sqlserver_datetime_round(v: datetime) -> datetime:
    """Round a timestamp the way SQL Server stores `datetime`: to the nearest 1/300 s,
    displayed in whole milliseconds (.000, .003, .007, .010, ...)."""
    ticks = (v.microsecond * 300 + 500_000) // 1_000_000
    ms = (ticks * 1000 + 150) // 300
    return v.replace(microsecond=0) + timedelta(milliseconds=ms)


# --------------------------------------------------------------------------- vectorised series

def _datetime_text(s: pl.Series, precision: str) -> pl.Series:
    """Datetime series -> canonical text series."""
    e = pl.col("t")
    if precision == "sqlserver_datetime":
        ticks = (e.dt.microsecond().cast(pl.Int64) * 300 + 500_000) // 1_000_000
        e = (e.dt.truncate("1s") + pl.duration(milliseconds=(ticks * 1000 + 150) // 300)).dt.strftime(_SQL_FMT)
    else:
        e = e.dt.strftime(_EXACT_FMT)
    return s.to_frame("t").select(e.alias("v")).to_series()


def _datetime_series(values: list, precision: str) -> pl.Series:
    try:
        if any(isinstance(v, datetime) and v.tzinfo is not None for v in values):
            raise TypeError("timezone-aware values")
        return _datetime_text(pl.Series(values, dtype=pl.Datetime("us"), strict=True), precision)
    except (TypeError, ValueError, OverflowError, pl.exceptions.PolarsError):
        pass
    # fallback: convert one by one, keep unparseable values visible as markers
    parsed: list[datetime | None] = []
    markers: dict[int, str] = {}
    for i, v in enumerate(values):
        if _is_null(v):
            parsed.append(None)
            continue
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v.strip())
            except ValueError:
                markers[i] = f"<not a timestamp: {v!r}>"
                parsed.append(None)
                continue
        if isinstance(v, datetime):
            parsed.append(v.replace(tzinfo=None))
        elif isinstance(v, date):
            parsed.append(datetime(v.year, v.month, v.day))
        else:
            markers[i] = f"<not a timestamp: {v!r}>"
            parsed.append(None)
    text = _datetime_text(pl.Series(parsed, dtype=pl.Datetime("us")), precision)
    if markers:
        lst = text.to_list()
        for i, m in markers.items():
            lst[i] = m
        text = pl.Series(lst, dtype=pl.String)
    return text


def _str_series(values: list) -> pl.Series:
    try:
        return pl.Series(values, dtype=pl.String, strict=True)
    except (TypeError, ValueError, pl.exceptions.PolarsError):
        return pl.Series([None if _is_null(v) else str(v) for v in values], dtype=pl.String)


def _int_series(values: list) -> pl.Series:
    if not any(isinstance(v, bool) for v in values):
        try:
            return pl.Series(values, dtype=pl.Int64, strict=True).cast(pl.String)
        except (TypeError, ValueError, OverflowError, pl.exceptions.PolarsError):
            pass
    return pl.Series([canon_int(v) for v in values], dtype=pl.String)


def canonical_series(values: list, col: ColumnSpec, side: str, aliases: Aliases, strict: bool,
                     precision: str = "exact") -> pl.Series:
    """Driver values of one column -> canonical text (pl.String)."""
    t = col.type
    if t == "str":
        s = _str_series(values)
        return s if strict else lenient_str(s, col.id)
    if t == "int":
        return _int_series(values)
    if t in ("decimal", "float"):
        return pl.Series([canon_decimal(v) for v in values], dtype=pl.String)
    if t == "datetime":
        return _datetime_series(values, precision)
    if t == "date":
        return pl.Series([canon_date(v) for v in values], dtype=pl.String)
    if t == "bool":
        return pl.Series([canon_bool(v) for v in values], dtype=pl.String)
    if t == "status":
        meanings = aliases.status[side]
        out = []
        for v in values:
            code = canon_int(v)
            if code is None or code.startswith("<"):
                out.append(code)
            else:
                out.append(meanings.get(int(code), f"code {code} (unmapped)"))
        return pl.Series(out, dtype=pl.String)
    if t == "downcode":
        s = _str_series(values)
        if not strict:
            s = lenient_str(s, False)
        return downcode_canonical(s, aliases)
    raise ValueError(f"unknown column type {t}")


def lenient_expr(e: pl.Expr, is_id: bool) -> pl.Expr:
    """Expression form of the lenient rule, so every caller normalises the same way."""
    e = e.str.strip_chars()
    e = pl.when(e == "").then(None).otherwise(e)
    if is_id:
        stripped = e.str.strip_chars_start("0")
        e = pl.when(e.is_not_null() & (stripped == "")).then(pl.lit("0")).otherwise(stripped)
    return e


def lenient_str(s: pl.Series, is_id: bool) -> pl.Series:
    """Trim, '' -> null and (for ids) strip leading zeros ('0001' -> '1', '000' -> '0')."""
    return s.to_frame("s").select(lenient_expr(pl.col("s"), is_id).alias(s.name)).to_series()


def downcode_canonical(s: pl.Series, aliases: Aliases) -> pl.Series:
    if not aliases.downcodes:
        return s
    low = pl.col("s").str.strip_chars().str.to_lowercase()
    e = pl.when(low.is_in(list(aliases.downcodes))).then(
        low.replace_strict(aliases.downcodes, default=None, return_dtype=pl.String)
    ).otherwise(pl.col("s"))
    return s.to_frame("s").select(e.alias(s.name)).to_series()


def pairing_key(s: pl.Series, col: ColumnSpec) -> pl.Series:
    """Key used only to *pair* records: always lenient, nulls become '<NULL>', so a record
    whose operator is ' 7' on one side and '7' on the other still pairs - and is then
    reported as a value difference by the strict comparison."""
    if col.type in ("str", "downcode"):
        s = lenient_str(s, col.id)
    return s.fill_null(NULL_KEY)
