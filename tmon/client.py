"""Fixed-host HTTPS transport. No generic URL or trading endpoint support."""

import http.client
import json
import math
import random
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from .errors import TmonError, invalid_data, safe_code

HOST = "openapi.tossinvest.com"
GROUPS = {
    "/api/v1/market-indicators/prices": "MARKET_INDICATOR",
    "/api/v1/rankings": "RANKING",
    "/api/v1/stocks/all": "STOCK_ALL",
    "/api/v1/prices": "MARKET_DATA",
    "/api/v1/candles": "MARKET_DATA_CHART",
    "/api/v1/stocks": "STOCK",
    "/api/v1/orderbook": "MARKET_DATA",
    "/api/v1/price-limits": "MARKET_DATA",
    "/api/v1/market-calendar/KR": "MARKET_INFO",
    "/api/v1/market-calendar/US": "MARKET_INFO",
}


def endpoint_group(path):
    if path in GROUPS:
        return GROUPS[path]
    if re.fullmatch(r"/api/v1/stocks/[A-Za-z0-9][A-Za-z0-9.\-]{0,63}/warnings", path):
        return "STOCK"
    if re.fullmatch(r"/api/v1/market-indicators/(KOSPI|KOSDAQ)/candles", path):
        return "MARKET_INDICATOR_CHART"
    if re.fullmatch(r"/api/v1/market-indicators/(KOSPI|KOSDAQ)/investor-trading", path):
        return "MARKET_INDICATOR"
    return None


class Transport:
    def __init__(self, budget=60, clock=None, sleeper=None, now=None):
        # Injected monotonic clock drives budget/elapsed; injected aware now()
        # drives observation timestamps. Defaults preserve live behavior.
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._now = now
        self.deadline = self._clock() + budget
        self.ready_at = {}
        # PR0 observations: one entry per request() call, success and failure.
        # Never stores headers, bodies, tokens or raw provider messages.
        self.observations = []

    def _wall_now(self):
        if self._now is not None:
            return self._now()
        return datetime.now(timezone.utc)

    def remaining(self):
        remaining = self.deadline - self._clock()
        if remaining <= 0:
            raise TmonError("deadline-exceeded", "명령의 60초 네트워크 예산을 초과했습니다.", 4, True)
        return remaining

    def pause(self, seconds):
        seconds = max(0, seconds)
        if seconds >= self.remaining():
            raise TmonError("retry-budget-exceeded", "권장 재시도 대기시간이 명령 예산을 초과합니다.", 4, True)
        self._sleep(seconds)

    def send(self, method, path, headers, body=None):
        connection = http.client.HTTPSConnection(HOST, timeout=min(5, self.remaining()),
                                                 context=ssl.create_default_context())
        try:
            connection.connect()
            connection.sock.settimeout(min(15, self.remaining()))
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            chunks = []
            size = 0
            while True:
                # Bound each read and the entire command, including slow responses.
                if connection.sock is not None:
                    connection.sock.settimeout(min(15, self.remaining()))
                else:
                    self.remaining()
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 10_000_000:
                    raise invalid_data("API 응답이 허용 크기를 초과했습니다.")
                chunks.append(chunk)
            self.remaining()
            try:
                data = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                if 200 <= status < 300:
                    raise invalid_data("API가 JSON 응답을 반환하지 않았습니다.")
                data = {}
            return status, response_headers, data
        except (OSError, socket.timeout, http.client.HTTPException):
            raise TmonError("network-error", "인증 서버 연결·DNS·TLS 또는 응답 수신에 실패했습니다.", 4, True) from None
        finally:
            connection.close()

    @staticmethod
    def seconds(value, default=0):
        try:
            result = float(value)
            return max(0, result) if math.isfinite(result) else default
        except (ValueError, TypeError):
            return default

    def retry_delay(self, headers, attempt):
        value = headers.get("retry-after")
        if value is not None:
            try:
                seconds = float(value)
                if math.isfinite(seconds):
                    return max(0, seconds) + random.uniform(0, 0.2)
            except ValueError:
                try:
                    date = parsedate_to_datetime(value)
                    return max(0, (date - datetime.now(timezone.utc)).total_seconds()) + random.uniform(0, 0.2)
                except (ValueError, TypeError, OverflowError):
                    pass
        return 2 ** attempt + random.uniform(0, 0.2)

    def request(self, method, path, params=None, token=None, form=None):
        group = endpoint_group(path) or "AUTH"
        started_wall = self._wall_now()
        started_mono = self._clock()
        rate_wait = 0.0
        retry_wait = 0.0
        attempts = 0
        recorded = False

        def observe(success, error_code=None):
            nonlocal recorded
            # Safe observation only: no headers, bodies, tokens or raw messages.
            # The code is normalized; unsafe provider strings never persist.
            recorded = True
            ended_wall = self._wall_now()
            elapsed = max(0.0, self._clock() - started_mono)
            entry = {'requestStartedAt': started_wall.isoformat(),
                     'receivedAt': ended_wall.isoformat(),
                     'endpointGroup': group, 'path': path,
                     'attemptCount': attempts,
                     'rateLimitWaitSeconds': round(rate_wait, 3),
                     'retryWaitSeconds': round(retry_wait, 3),
                     'elapsedSeconds': round(elapsed, 3),
                     'success': success, 'errorCode': safe_code(error_code) if not success else None}
            self.observations.append(entry)
            return entry

        def metered(label, seconds):
            # Measure ACTUAL waited time around pause, including refusal or
            # interruption (which waits nothing). Never book planned delays.
            nonlocal rate_wait, retry_wait
            start = self._clock()
            try:
                self.pause(seconds)
            finally:
                actual = round(max(0.0, self._clock() - start), 3)
                if label == 'rate':
                    rate_wait += actual
                else:
                    retry_wait += actual

        if not ((method == "GET" and endpoint_group(path) is not None) or
                (method == "POST" and path == "/oauth2/token")):
            observe(False, "unsupported-endpoint")
            raise TmonError("unsupported-endpoint", "조회 CLI에서 허용하지 않는 API입니다.", 2)
        url = path + ("?" + urlencode(params) if params else "")
        headers = {"Accept": "application/json", "User-Agent": "tmon/0.1"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            for attempt in range(3 if method == "GET" else 1):
                wait = self.ready_at.get(group, 0) - self._clock()
                if wait > 0:
                    metered('rate', wait)
                attempts += 1
                try:
                    status, response_headers, data = self.send(method, url, headers, body)
                except TmonError as error:
                    if method == "GET" and error.code == "network-error" and attempt < 2:
                        metered('retry', self.retry_delay({}, attempt))
                        continue
                    observe(False, error.code)
                    raise
                if response_headers.get("x-ratelimit-remaining") == "0":
                    self.ready_at[group] = self._clock() + self.seconds(response_headers.get("x-ratelimit-reset"))
                if group == "STOCK_ALL":
                    self.ready_at[group] = max(self.ready_at.get(group, 0), self._clock() + 1)
                if method == "GET" and (status == 429 or status in (500, 502, 503, 504)) and attempt < 2:
                    metered('retry', self.retry_delay(response_headers, attempt))
                    continue
                if not 200 <= status < 300:
                    error = api_error(status, response_headers, data)
                    observe(False, error.code)
                    raise error
                if not isinstance(data, dict):
                    observe(False, "invalid-data")
                    raise invalid_data()
                observe(True, None)
                return data
        except TmonError as error:
            if not recorded:
                observe(False, getattr(error, 'code', None))
            raise
        except KeyboardInterrupt:
            # Interruption waits nothing beyond what was actually metered.
            if not recorded:
                observe(False, "interrupted")
            raise
        except Exception:
            # Never record raw exception messages.
            if not recorded:
                observe(False, "internal-error")
            raise TmonError("internal-error", "예상하지 못한 내부 오류입니다.", 1)


def api_error(status, headers, data):
    raw = data.get("error", {}) if isinstance(data, dict) else {}
    code = raw.get("code") if isinstance(raw, dict) else raw
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", code):
        code = "api-error"
    request_id = headers.get("x-request-id")
    if not request_id and isinstance(raw, dict):
        request_id = raw.get("requestId")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        request_id = None
    if status in (401, 403):
        message, exit_code = "인증 또는 접근 권한 오류입니다. 환경 변수와 허용 IP·권한을 확인하세요.", 3
    elif status == 404:
        message, exit_code = "요청한 종목 또는 데이터를 찾을 수 없습니다.", 5
    elif status == 400:
        message, exit_code = "API가 요청 조건을 거부했습니다.", 2
    else:
        message, exit_code = "API 요청에 실패했습니다 (HTTP %s)." % status, 4
    # Never surface provider messages, bodies or request headers.
    return TmonError(code, message, exit_code, status == 429 or status >= 500, request_id)


class TossClient:
    def __init__(self, auth, transport):
        self.auth, self.transport = auth, transport

    def get(self, path, **params):
        if endpoint_group(path) is None:
            raise TmonError("unsupported-endpoint", "조회 CLI에서 허용하지 않는 API입니다.", 2)
        token = self.auth.token()
        try:
            data = self.transport.request("GET", path, params, token)
        except TmonError as error:
            if error.code not in ("invalid-token", "expired-token"):
                raise
            token = self.auth.token(rejected=token)
            data = self.transport.request("GET", path, params, token)
        if "result" not in data:
            raise invalid_data()
        return data["result"]
