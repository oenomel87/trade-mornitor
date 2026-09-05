"""Cached, read-only instrument catalogue search."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

from .auth import cache_directory, secure_directory, secure_open
from .errors import TmonError, invalid_data
from .market import timestamp

KR_MARKETS = ("KOSPI", "KOSDAQ", "KR_ETC")
US_MARKETS = ("NYSE", "NASDAQ", "AMEX", "US_ETC")
MARKETS = KR_MARKETS + US_MARKETS
MARKET_CHOICES = ("ALL", "KR", "US") + MARKETS
ALIASES = {"apple": "AAPL", "현대자동차": "005380", "hyundaimotor": "005380",
           "samsungelectronics": "005930"}


def normalized(text):
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def validate_query(query, limit):
    if not normalized(query) or len(query) > 200 or any(unicodedata.category(c).startswith("C") for c in query):
        raise TmonError("invalid-query", "검색어는 제어 문자를 제외한 1~200자로 입력하세요.", 2)
    if not 1 <= limit <= 200:
        raise TmonError("invalid-limit", "--limit는 1~200이어야 합니다.", 2)


def selected_markets(selection):
    if selection not in MARKET_CHOICES:
        raise TmonError("invalid-market", "지원하지 않는 검색 시장입니다.", 2)
    return MARKETS if selection == "ALL" else KR_MARKETS if selection == "KR" else US_MARKETS if selection == "US" else (selection,)


def validate_rows(raw):
    if not isinstance(raw, list):
        raise invalid_data("종목 목록 형식이 올바르지 않습니다.")
    rows, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise invalid_data("종목 목록 형식이 올바르지 않습니다.")
        symbol, name, kind = row.get("symbol"), row.get("name"), row.get("securityType")
        # Some real names contain trailing tabs; collapse ordinary whitespace
        # without admitting terminal escape/control sequences into the table.
        if isinstance(name, str):
            name = " ".join(name.split())
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,63}", symbol)
                or not isinstance(name, str) or not name.strip() or len(name) > 500
                or any(unicodedata.category(c).startswith("C") for c in name)
                or not isinstance(kind, str) or not re.fullmatch(r"[A-Z_]{1,80}", kind)
                or not isinstance(row.get("isCommonShare"), bool)):
            raise invalid_data("종목 목록의 필수 필드가 올바르지 않습니다.")
        symbol = symbol.upper()
        if symbol in seen:
            raise invalid_data("동일 시장 종목 목록에 중복 코드가 있습니다.")
        seen.add(symbol)
        rows.append({"symbol": symbol, "name": name, "securityType": kind,
                     "isCommonShare": row["isCommonShare"]})
    return rows


class Catalogue:
    def __init__(self, client_factory, transport, directory=None, now=None):
        self.client_factory, self.transport = client_factory, transport
        self.directory = Path(directory) if directory else cache_directory() / "stocks-v1"
        self.now = now
        self.client = None

    def current_time(self):
        return self.now or datetime.now(timezone.utc)

    @contextmanager
    def locked(self):
        secure_directory(self.directory)
        fd = secure_open(self.directory / "refresh.lock", os.O_CREAT | os.O_RDWR)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.transport.pause(0.05)
            yield
        finally:
            os.close(fd)

    def read(self, market):
        try:
            path = self.directory / (market + ".json")
            # Read-only cache hits work without write permission or authentication.
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 10_000_000:
                    return None
                raw = json.load(handle)
            if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("market") != market:
                return None
            fetched = timestamp(raw["fetchedAt"])
            if fetched > self.current_time():
                return None
            return {"rows": validate_rows(raw["rows"]), "fetchedAt": fetched.isoformat()}
        except (FileNotFoundError, ValueError, KeyError, TypeError, TmonError):
            return None

    def fresh(self, cached):
        return cached is not None and self.current_time() - timestamp(cached["fetchedAt"]) < timedelta(hours=24)

    def write(self, market, rows):
        data = {"version": 1, "market": market, "fetchedAt": self.current_time().isoformat(), "rows": rows}
        fd, temporary = tempfile.mkstemp(prefix=".stocks-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / (market + ".json"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return data

    def load(self, markets, refresh=False):
        all_rows, cache_meta, warnings = [], [], []
        try:
            for market in markets:
                cached = self.read(market)
                stale, hit = False, True
                if refresh or not self.fresh(cached):
                    with self.locked():
                        cached = self.read(market)
                        if refresh or not self.fresh(cached):
                            try:
                                if self.client is None:
                                    self.client = self.client_factory()
                                rows = validate_rows(self.client.get("/api/v1/stocks/all", market=market, status="ACTIVE"))
                            except TmonError as error:
                                if refresh or cached is None or error.exit_code != 4:
                                    raise
                                stale = True
                                warnings.append({"code": "stale-catalogue", "message":
                                    "%s 목록 갱신 실패로 %s 캐시를 사용합니다 (%s)." % (market, cached["fetchedAt"], error.code)})
                            else:
                                cached = self.write(market, rows)
                                hit = False
                all_rows.extend(dict(row, market=market) for row in cached["rows"])
                cache_meta.append({"market": market, "fetchedAt": cached["fetchedAt"], "cacheHit": hit,
                                   "stale": stale, "count": len(cached["rows"])})
        except OSError:
            raise TmonError("catalogue-cache-unavailable", "종목 목록 캐시의 경로·권한을 확인하세요.", 2) from None
        return all_rows, cache_meta, warnings


def search(catalogue, query, selection="ALL", limit=20, refresh=False):
    validate_query(query, limit)
    markets = selected_markets(selection)
    rows, cache_meta, warnings = catalogue.load(markets, refresh)
    needle = normalized(query)
    alias = ALIASES.get(needle)
    matches = []
    for row in rows:
        code, name = normalized(row["symbol"]), normalized(row["name"])
        if needle in (code, name):
            priority = 0
        elif row["symbol"] == alias:
            priority = 1
        elif code.startswith(needle) or name.startswith(needle):
            priority = 2
        elif needle in code or needle in name:
            priority = 3
        else:
            continue
        matches.append((priority, name, row["market"], row["symbol"], row))
    matches.sort(key=lambda match: match[:4])
    meta = {"query": query, "markets": list(markets), "totalMatches": len(matches),
            "returnedCount": min(limit, len(matches)), "truncated": len(matches) > limit,
            "catalogue": cache_meta}
    return [match[4] for match in matches[:limit]], meta, warnings, 5 if warnings else 0
