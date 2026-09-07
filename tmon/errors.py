import re

_SAFE_CODE = re.compile(r"[a-z][a-z0-9_-]{0,79}")


class TmonError(Exception):
    def __init__(self, code, message, exit_code=5, retryable=False, request_id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
        self.request_id = request_id

    def as_dict(self):
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "requestId": self.request_id}


def invalid_data(message="공급자 응답의 데이터 형식을 확인할 수 없습니다."):
    return TmonError("invalid-data", message)


def safe_code(code):
    """Normalize an error code for audit records.

    Only the existing safe pattern is stored; anything else (including raw
    provider strings) becomes 'api-error'. Raw messages are never stored.
    """
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "api-error"


# PR0: evidence-based classification for expected ineligibility.
# Only listing-history-too-short carries listing-date evidence that the
# required daily bars could never have existed. Generic codes such as
# insufficient-history, stale-daily, incomplete-bars and zero-volume-baseline
# have no such evidence and must remain candidate-data-unavailable.
EXPECTED_EVIDENCE_FIELDS = ('listDate', 'requiredDailyBars', 'maxPossibleDailyBars')

SESSION_NOT_READY_EVIDENCE_FIELDS = ('sessionStart', 'phaseStartedAt',
                                     'requiredCompletedBars', 'completedBars')


def is_expected_ineligible(error):
    """True only for evidence-backed expected ineligibility.

    Currently the sole expected code is listing-history-too-short with
    listDate/requiredDailyBars/maxPossibleDailyBars details. Everything else,
    including generic history/staleness/contiguity failures, returns False.
    """
    if getattr(error, 'code', None) == 'listing-history-too-short':
        details = getattr(error, 'details', None)
        if not isinstance(details, dict):
            return False
        return all(details.get(field) is not None for field in EXPECTED_EVIDENCE_FIELDS)
    if getattr(error, 'code', None) == 'session-not-ready':
        # Expected evaluation unavailability: session exists but the required
        # 6 completed 5-minute bars cannot exist yet. Needs session start, P,
        # required 6 and completed n evidence; otherwise not expected.
        details = getattr(error, 'details', None)
        if not isinstance(details, dict):
            return False
        return all(details.get(field) is not None for field in SESSION_NOT_READY_EVIDENCE_FIELDS)
    return False
