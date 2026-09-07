"""Bounded batch prechecks for recommendation candidates.

The recommendation engine ranks and orders the universe before calling this
module.  This module only verifies the provider's stock payload, applies the
existing stock eligibility policy, and returns enough state for screening to
reuse the response.  Final validation must still make a fresh stock request.
"""

from copy import deepcopy
from datetime import datetime

from .errors import TmonError, is_expected_ineligible, safe_code
from .intraday import utcnow
from .market import timestamp
from .recommend_data import stock_eligibility
from .strategies import NoMatch


MAX_STOCK_BATCH = 200
# A descriptive alias is useful to callers that want to validate their own
# provider limit without importing an implementation-specific name.
STOCK_BATCH_LIMIT = MAX_STOCK_BATCH


def _is_auth_error(error):
    """Return whether *error* must abort the complete recommendation run."""
    # The existing client contract reserves exit code 3 for authentication or
    # access failures.  Do not infer fatality from arbitrary provider codes.
    return getattr(error, "exit_code", None) == 3


def _is_budget_error(error):
    """Recognize a budget refusal from either Engine or Transport."""
    code = getattr(error, "code", None)
    return (code in {"recommend-budget", "deadline-exceeded", "retry-budget-exceeded",
                     "budget-exceeded"} or
            (getattr(error, "exit_code", None) == 4 and
             isinstance(code, str) and "budget" in code))


def _as_iso(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _record_base(candidate, reason, *, kind, message, details=None):
    """Build an exclusion/skip record without losing ranking metadata."""
    symbol = candidate.get("symbol") if isinstance(candidate, dict) else candidate
    record = {
        "symbol": symbol,
        "reason": reason,
        "stage": "precheck",
        "kind": kind,
        "expectedIneligible": False,
        "message": message,
        # Keep the complete candidate available to an integrator while also
        # exposing the common ranking fields at the record's top level.
        "candidate": deepcopy(candidate),
    }
    if isinstance(candidate, dict):
        for key in ("preRank", "ranks"):
            if key in candidate:
                record[key] = deepcopy(candidate[key])
    if details:
        detail_copy = deepcopy(details)
        record.update(detail_copy)
        record["details"] = detail_copy
        # Response evidence cannot change the record's classification or
        # candidate identity.
        record.update(symbol=symbol, reason=reason, stage="precheck", kind=kind,
                      expectedIneligible=False, message=message,
                      candidate=deepcopy(candidate))
    return record


def _condition_record(candidate, error):
    details = getattr(error, "details", None)
    record = _record_base(candidate, error.code, kind="condition",
                          message=error.message, details=details)
    record["expectedIneligible"] = is_expected_ineligible(error)
    return record


def _data_record(candidate, reason, message, details=None):
    return _record_base(candidate, safe_code(reason), kind="data",
                        message=message, details=details)


def _candidate_symbol(candidate):
    if isinstance(candidate, dict):
        return candidate.get("symbol")
    return candidate


def _received_at(client, now):
    """Get the response receive marker without inventing a reuse timestamp.

    RecordingClient already marks the end of a logical request.  A plain fake
    client has no such ledger, so call the injected wall clock immediately
    after its ``get`` returns.  In both cases the value is captured once per
    batch and copied to each symbol in that batch.
    """
    records = getattr(client, "records", None)
    if isinstance(records, list) and records:
        record = records[-1]
        if (isinstance(record, dict) and record.get("path") == "/api/v1/stocks" and
                record.get("receivedAt") is not None):
            return record["receivedAt"]
    return now()


def _budget_available(client, checker):
    """Check budget without issuing a request.

    ``checker`` may be a boolean-returning callback or an Engine-style
    callback that raises ``TmonError`` when the deadline has passed.  When no
    callback is supplied, a Transport's ``remaining`` method is consulted if
    available; ordinary fakes have no budget boundary and are allowed.
    """
    callback = checker
    if callback is None:
        transport = getattr(client, "transport", None)
        callback = getattr(transport, "remaining", None)
    if callback is None:
        inner = getattr(client, "client", None)
        transport = getattr(inner, "transport", None)
        callback = getattr(transport, "remaining", None)
    if callback is None:
        callback = getattr(client, "remaining", None)
    if callback is None:
        return True
    try:
        value = callback() if callable(callback) else callback
    except TmonError as error:
        if _is_auth_error(error):
            raise
        return False
    if value is None:
        # Engine.check_budget() uses an exception-only convention: returning
        # means the check passed, while exhaustion raises TmonError.
        return True
    if isinstance(value, bool):
        return value
    try:
        return value > 0
    except TypeError:
        return bool(value)


def _candidate_rows(candidates):
    """Return candidates in caller-provided preRank order, de-duplicated.

    The normal ranking union is already unique.  The defensive de-duplication
    here makes the one-request-per-symbol guarantee hold for simple fakes and
    hand-built integration inputs while retaining the original order.
    """
    rows = []
    seen = set()
    for candidate in candidates or []:
        symbol = _candidate_symbol(candidate)
        if not isinstance(symbol, str) or not symbol:
            # Keep malformed rows so the caller gets an explicit data record;
            # they cannot be sent to the provider and are handled by the
            # precheck loop before batching.
            rows.append(candidate)
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append(candidate)
    return rows


def _batch_issues(raw, requested):
    """Validate response shape and return ``(mapping, issues)``.

    An unrequested or malformed row invalidates the complete batch.  This is
    deliberate: assigning by list position after a provider mapping defect
    could attach one company's eligibility data to another symbol.  Missing
    and duplicate requested symbols are localized so unaffected rows remain
    usable, while the affected symbols become data-unavailable.
    """
    if not isinstance(raw, list):
        return {}, [{"reason": "invalid-data", "details": {
            "responseType": type(raw).__name__,
            "requestedSymbols": list(requested),
        }}]

    mapping = {}
    malformed = []
    received_symbols = []
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or not row.get("symbol"):
            malformed.append(row if isinstance(row, dict) else {"type": type(row).__name__})
            continue
        symbol = row["symbol"]
        received_symbols.append(symbol)
        mapping.setdefault(symbol, []).append(row)

    requested_set = set(requested)
    extra = sorted(set(received_symbols) - requested_set)
    if malformed or extra:
        return {}, [{
            "reason": "unrequested-symbol" if extra else "invalid-data",
            "details": {
                "requestedSymbols": list(requested),
                "receivedSymbols": list(received_symbols),
                "unrequestedSymbols": extra,
                "malformedRowCount": len(malformed),
            },
        }]

    issues = []
    for symbol in requested:
        count = len(mapping.get(symbol, []))
        if count == 0:
            issues.append({"symbol": symbol, "reason": "missing-symbol",
                           "details": {"requestedSymbols": list(requested),
                                        "receivedSymbols": list(received_symbols)}})
        elif count > 1:
            issues.append({"symbol": symbol, "reason": "duplicate-symbol",
                           "details": {"duplicateCount": count,
                                        "requestedSymbols": list(requested)}})
    clean = {symbol: rows[0] for symbol, rows in mapping.items()
             if symbol in requested_set and len(rows) == 1}
    return clean, issues


def _empty_result(universe_count, detail_limit):
    return {
        "universeCount": universe_count,
        "detailLimit": detail_limit,
        "eligibleRows": [],
        "selectedRows": [],
        "stockRaw": {},
        "receivedAtBySymbol": {},
        "batches": [],
        "preExcluded": [],
        "dataUnavailable": [],
        "precheckDataUnavailableRecords": [],
        "precheckDataUnavailable": 0,
        "notEvaluated": [],
    }


def precheck_universe(client, candidates, horizon, detail_limit, now=utcnow,
                      budget_remaining=None, *, budget_check=None,
                      trading_dates=None):
    """Precheck an ordered ranking union using bounded stock batches.

    Parameters
    ----------
    client:
        An actual ``RecordingClient`` or a compatible object with ``get``.
    candidates:
        Candidate dicts in their established descending ``preRank`` order.
    horizon:
        ``day`` or ``swing``; forwarded to :func:`stock_eligibility`.
    detail_limit:
        Applied only after every candidate that has a successful precheck.
    now:
        Injected aware wall-clock callback.  It is sampled once after each
        plain client's response; a RecordingClient's own response marker is
        reused instead.
    budget_remaining / budget_check:
        Optional callback checked before *every* provider request.  The two
        names are accepted for Engine and standalone callers respectively.

    Returns a dict containing ``selectedRows`` (the first detail-limit rows
    among ``eligibleRows``), a single-symbol ``stockRaw`` mapping suitable for
    ``current_data(..., preloaded_stock=...)``, per-batch receive metadata,
    condition-only ``preExcluded`` records, data-only ``dataUnavailable``
    records, and budget/cap ``notEvaluated`` records.  No request is made for
    an empty candidate union.
    """
    source_candidates = list(candidates or [])
    ordered = _candidate_rows(source_candidates)
    try:
        cap = max(0, int(detail_limit)) if detail_limit is not None else None
    except (TypeError, ValueError):
        raise ValueError("detail_limit must be an integer or None") from None
    # Preserve the caller's universe count.  Normal ranking unions are unique,
    # but a duplicate supplied by an integration test must not silently alter
    # the count recorded for the original union.
    result = _empty_result(len(source_candidates), cap)
    if not ordered:
        return result

    valid_rows = []
    valid_symbols = []
    # Invalid candidate symbols never reach the provider.  They are data
    # unavailable and are kept distinct from provider-confirmed conditions.
    for candidate in ordered:
        symbol = _candidate_symbol(candidate)
        if not isinstance(symbol, str) or not symbol:
            result["dataUnavailable"].append(
                _data_record(candidate, "invalid-symbol", "랭킹 후보의 종목 식별자를 확인할 수 없습니다."))
            continue
        valid_rows.append(candidate)
        valid_symbols.append(symbol)

    if not valid_rows:
        result["precheckDataUnavailableRecords"] = result["dataUnavailable"]
        result["precheckDataUnavailable"] = len(result["dataUnavailable"])
        return result

    for start in range(0, len(valid_rows), MAX_STOCK_BATCH):
        batch_rows = valid_rows[start:start + MAX_STOCK_BATCH]
        batch_symbols = valid_symbols[start:start + MAX_STOCK_BATCH]
        checker = budget_remaining if budget_remaining is not None else budget_check
        if not _budget_available(client, checker):
            for candidate in valid_rows[start:]:
                result["notEvaluated"].append(
                    _record_base(candidate, "not-evaluated-budget", kind="coverage",
                                 message="사전 종목 정보 조회 예산이 없어 평가하지 않았습니다."))
            break

        params = {"symbols": ",".join(batch_symbols)}
        try:
            raw = client.get("/api/v1/stocks", **params)
        except TmonError as error:
            if _is_auth_error(error):
                raise
            received = _received_at(client, now)
            received_iso = _as_iso(received)
            for symbol in batch_symbols:
                result["receivedAtBySymbol"][symbol] = received_iso
            result["batches"].append({"symbols": list(batch_symbols),
                                      "receivedAt": received_iso,
                                      "success": False,
                                      "errorCode": safe_code(getattr(error, "code", None))})
            if _is_budget_error(error):
                for candidate in valid_rows[start:]:
                    result["notEvaluated"].append(
                        _record_base(candidate, "not-evaluated-budget", kind="coverage",
                                     message="사전 종목 정보 조회 예산이 없어 평가하지 않았습니다."))
                break
            for candidate in batch_rows:
                result["dataUnavailable"].append(
                    _data_record(candidate, getattr(error, "code", "api-error"),
                                 "사전 종목 정보 조회에 실패해 평가하지 못했습니다.",
                                 {"batchSymbols": list(batch_symbols)}))
            continue
        except Exception:
            # Do not persist an arbitrary provider/library exception message.
            received = _received_at(client, now)
            received_iso = _as_iso(received)
            for symbol in batch_symbols:
                result["receivedAtBySymbol"][symbol] = received_iso
            result["batches"].append({"symbols": list(batch_symbols),
                                      "receivedAt": received_iso,
                                      "success": False,
                                      "errorCode": "internal-error"})
            for candidate in batch_rows:
                result["dataUnavailable"].append(
                    _data_record(candidate, "internal-error",
                                 "사전 종목 정보를 처리하지 못했습니다.",
                                 {"batchSymbols": list(batch_symbols)}))
            continue

        received = _received_at(client, now)
        received_iso = _as_iso(received)
        result["batches"].append({"symbols": list(batch_symbols),
                                  "receivedAt": received_iso,
                                  "success": True})
        for symbol in batch_symbols:
            result["receivedAtBySymbol"][symbol] = received_iso

        mapping, issues = _batch_issues(raw, batch_symbols)
        if issues and any("symbol" not in issue for issue in issues):
            # A malformed/unrequested row compromises all positional or
            # inferred assignments in this provider response.
            details = issues[0].get("details", {})
            reason = issues[0]["reason"]
            for candidate in batch_rows:
                result["dataUnavailable"].append(
                    _data_record(candidate, reason,
                                 "사전 종목 정보 응답의 종목 매핑을 확인할 수 없습니다.", details))
            continue

        issue_by_symbol = {issue["symbol"]: issue for issue in issues}
        by_symbol = {symbol: candidate for symbol, candidate in zip(batch_symbols, batch_rows)}
        for symbol in batch_symbols:
            candidate = by_symbol[symbol]
            issue = issue_by_symbol.get(symbol)
            if issue is not None:
                result["dataUnavailable"].append(
                    _data_record(candidate, issue["reason"],
                                 "사전 종목 정보 응답에서 종목을 확인할 수 없습니다.",
                                 issue.get("details")))
                continue
            row = mapping.get(symbol)
            if row is None:
                # Defensive fallback; _batch_issues should have emitted a
                # missing-symbol issue, but never silently skip a symbol.
                result["dataUnavailable"].append(
                    _data_record(candidate, "missing-symbol",
                                 "사전 종목 정보 응답에서 종목을 확인할 수 없습니다.",
                                 {"requestedSymbols": list(batch_symbols)}))
                continue
            if isinstance(received, datetime):
                eligibility_now = received
            else:
                try:
                    eligibility_now = timestamp(received)
                except (TypeError, ValueError, OverflowError):
                    eligibility_now = now()
            try:
                stock_eligibility([row], symbol, horizon, eligibility_now,
                                  trading_dates=trading_dates)
            except TmonError as error:
                if _is_auth_error(error):
                    raise
                if isinstance(error, NoMatch):
                    result["preExcluded"].append(_condition_record(candidate, error))
                else:
                    result["dataUnavailable"].append(
                        _data_record(candidate, getattr(error, "code", "invalid-data"),
                                     "사전 종목 정보가 유효하지 않아 평가하지 못했습니다.",
                                     getattr(error, "details", None)))
                continue
            # current_data expects a list-shaped stock response.  Copy the
            # individual row so later evaluation cannot mutate this snapshot.
            result["stockRaw"][symbol] = [deepcopy(row)]
            valid = deepcopy(candidate)
            result["eligibleRows"].append(valid)

    result["precheckDataUnavailableRecords"] = result["dataUnavailable"]
    result["precheckDataUnavailable"] = len(result["dataUnavailable"])

    # Only passed prechecks consume detailLimit.  Condition and data failures
    # do not create cap records, avoiding double-counting in summary metrics.
    eligible = result["eligibleRows"]
    selected = eligible if cap is None else eligible[:cap]
    result["selectedRows"] = deepcopy(selected)
    if cap is not None:
        for candidate in eligible[cap:]:
            result["notEvaluated"].append(
                _record_base(candidate, "detail-limit", kind="coverage",
                             message="사전 확인을 통과했지만 상세평가 한도를 초과했습니다."))
    # A budget stop may be discovered before the cap records are appended.
    # Restore source order so coverage output remains easy to reconcile with
    # the ranking union.
    positions = {symbol: index for index, symbol in enumerate(valid_symbols)}
    result["notEvaluated"].sort(
        key=lambda record: positions.get(record.get("symbol"), len(positions)))
    return result


__all__ = ["MAX_STOCK_BATCH", "STOCK_BATCH_LIMIT", "precheck_universe"]
