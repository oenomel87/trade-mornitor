import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import sys
import unicodedata

from .analysis import analyze
from .auth import Auth, local_checks
from .client import TossClient, Transport
from .errors import TmonError
from .market import history, quote, symbols
from .search import Catalogue, MARKET_CHOICES, search, validate_query
from .ranking import DURATIONS, METRICS, METRIC_LABELS, SOURCES, rank, validate_rank
from .watchlist import Watchlist, run_watchlist


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse can echo invalid input, which might accidentally be a secret.
        raise TmonError("invalid-arguments", "명령·인수·옵션을 확인하세요. 사용법: python3 -m tmon --help", 2)


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON 객체 하나로 출력")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="색상 없는 출력 (기본값)")
    root = Parser(prog="tmon", description="토스증권 조회·분석 전용 CLI (매매 기능 없음)", parents=[common])
    commands = root.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", parents=[common], help="설정·연결 진단")
    doctor.add_argument("--remote", action="store_true", help="시세 API 연결 확인. 새 토큰 발급 시 기존 토큰 무효화")
    q = commands.add_parser("quote", parents=[common], help="최대 200종목 현재가")
    q.add_argument("symbols", nargs="+", metavar="SYMBOL")
    h = commands.add_parser("history", parents=[common], help="완료된 일봉 조회")
    h.add_argument("symbol", metavar="SYMBOL")
    h.add_argument("--count", type=int, default=20, help="1~200봉 (기본 20)")
    h.add_argument("--unadjusted", action="store_true", help="수정주가 미적용")
    a = commands.add_parser("analyze", parents=[common], help="SMA20·60, RSI14, 거래량 배수")
    a.add_argument("symbol", metavar="SYMBOL")
    rec = commands.add_parser("recommend", parents=[common], help="국내 당일·2~5거래일 돌파 후보 추천")
    rec.add_argument("--horizon", required=True, choices=("day", "swing"))
    rec.add_argument("--market", default="KR", type=str.upper, choices=("KR",))
    rec.add_argument("--limit", type=int, default=3)
    rec.add_argument("--capital", help="종목당 가정 투입 원화 금액")
    rec.add_argument("--max-loss-pct", help="진입가 대비 무효화 가격 거리 상한 (%%)")
    rec.add_argument("--research", choices=("auto", "off"), default=None, help="기본 auto: 기존 ChatGPT 로그인으로 웹 조사")
    rec.add_argument("--config", help="추천 설정 JSON 파일")
    s = commands.add_parser("search", parents=[common], help="종목명·코드 검색 (24시간 캐시)")
    s.add_argument("query", metavar="QUERY")
    s.add_argument("--market", type=str.upper, choices=MARKET_CHOICES, default="ALL", help="검색 시장 (기본 ALL)")
    s.add_argument("--limit", type=int, default=20, help="출력 개수 1~200 (기본 20)")
    s.add_argument("--refresh", action="store_true", help="캐시 대신 API에서 종목 목록을 다시 조회")
    r = commands.add_parser("rank", parents=[common], help="국내·미국 거래대금·거래량·등락률 랭킹")
    r.add_argument("--market", type=str.upper, choices=("KR", "US"), default="KR", help="시장 (기본 KR)")
    r.add_argument("--by", type=str.lower, choices=METRICS, default="amount", help="순위 기준 (기본 amount)")
    r.add_argument("--duration", type=str.lower, choices=DURATIONS, default="1d", help="기간 (기본 1d, gain/loss는 realtime 불가)")
    r.add_argument("--source", type=str.lower, choices=SOURCES, default="market", help="시장 전체 market / 토스증권 체결 toss (기본 market)")
    r.add_argument("--limit", type=int, default=20, help="조회 개수 1~100 (기본 20)")
    r.add_argument("--exclude-caution", action="store_true", help="투자 유의 종목 제외")
    selection = argparse.ArgumentParser(add_help=False)
    selection.add_argument("--profile", default=argparse.SUPPRESS, metavar="NAME", help="이번 명령의 대상 프로필 (생략 시 현재 선택)")
    w = commands.add_parser("watchlist", parents=[common, selection], help="프로필별 관심종목 저장·삭제·목록·묶음 현재가")
    w.set_defaults(action="list")
    actions = w.add_subparsers(dest="watchlist_action")
    for action, help_text in (("add", "코드 추가 (존재 여부는 API에서 확인하지 않음)"),
                              ("remove", "지정 코드 삭제"), ("list", "저장 순서대로 목록 표시"),
                              ("quote", "저장한 모든 종목의 현재가 조회")):
        sub = actions.add_parser(action, parents=[common, selection], help=help_text)
        sub.set_defaults(action=action)
        if action in ("add", "remove"):
            sub.add_argument("symbols", nargs="+", metavar="SYMBOL")
    p = commands.add_parser("profile", parents=[common], help="관심종목 프로필 생성·선택·이름 변경·삭제")
    p.set_defaults(action="list")
    actions = p.add_subparsers(dest="profile_action")
    for action, help_text in (("list", "프로필 목록과 현재 선택"), ("create", "빈 프로필 생성 (현재 선택 유지)"),
                              ("use", "현재 프로필 선택 (모든 터미널에 공유)"),
                              ("rename", "프로필 이름 변경"), ("delete", "선택되지 않은 프로필 삭제")):
        sub = actions.add_parser(action, parents=[common], help=help_text)
        sub.set_defaults(action=action)
        if action != "list":
            sub.add_argument("name", metavar="NAME")
        if action == "rename":
            sub.add_argument("new_name", metavar="NEW")
        if action == "delete":
            sub.add_argument("--force", action="store_true", help="종목이 있는 비선택 프로필도 삭제")
    return root


def serialize(value):
    if isinstance(value, Decimal):
        with localcontext() as context:
            context.prec = 80
            rounded = value.quantize(Decimal("0.00000001"))
            if rounded == 0:
                return "0"
            return format(rounded, "f").rstrip("0").rstrip(".")
    raise TypeError("unsupported output type")


def envelope(command):
    return {"schemaVersion": 1, "command": command, "status": "ok", "data": None,
            "meta": {"queriedAt": datetime.now(timezone.utc).isoformat(), "source": "Toss Securities Open API"},
            "warnings": [], "error": None}


def run(args, result):
    if args.command == "recommend":
        from .recommend import run_recommend
        return run_recommend(args, result)
    if args.command == "profile":
        store = Watchlist()
        result["meta"].update(source="local", action=args.action, watchlistFile=str(store.path))
        data, meta, warnings, code = store.profile(args.action, getattr(args, "name", None),
                                                  getattr(args, "new_name", None), getattr(args, "force", False))
        result["data"], result["warnings"] = data, warnings
        result["meta"].update(meta)
        return code
    if args.command == "watchlist":
        def client_factory():
            result["meta"]["source"] = "Toss Securities Open API"
            transport = Transport()
            return TossClient(Auth(transport), transport)
        store = Watchlist()
        result["meta"]["source"] = "local"
        result["meta"].update(action=args.action, watchlistFile=str(store.path))
        data, meta, warnings, code = run_watchlist(store, args.action, getattr(args, "symbols", None), client_factory,
                                                  getattr(args, "profile", None), result["meta"])
        result["data"], result["warnings"] = data, warnings
        result["meta"].update(meta)
        if args.action == "quote" and meta["savedCount"]:
            result["meta"]["source"] = "Toss Securities Open API"
        result["status"] = "partial" if code else "ok"
        return code
    if args.command == "search":
        validate_query(args.query, args.limit)
        transport = Transport()
        catalogue = Catalogue(lambda: TossClient(Auth(transport), transport), transport)
        data, meta, warnings, code = search(catalogue, args.query, args.market, args.limit, args.refresh)
        result["data"], result["warnings"] = data, warnings
        result["meta"].update(meta)
        result["status"] = "partial" if code else "ok"
        return code
    if args.command == "quote":
        args.symbols = symbols(args.symbols)
    elif args.command == "rank":
        validate_rank(args.market, args.by, args.duration, args.source, args.limit)
    elif args.command in ("history", "analyze"):
        args.symbol = symbols([args.symbol])[0]
        if args.command == "history" and not 1 <= args.count <= 200:
            raise TmonError("invalid-count", "--count는 1~200이어야 합니다.", 2)
    if args.command == "doctor":
        checks = local_checks()
        result["data"] = checks
        result["meta"]["checks"] = checks
        result["meta"]["source"] = "local"
        if any(value in ("누락", "실패") for value in checks.values()):
            raise TmonError("configuration-error", "환경 변수 또는 토큰 캐시 경로를 확인하세요.", 2)
        if not args.remote:
            return 0
    transport = Transport()
    client = TossClient(Auth(transport), transport)
    result["meta"]["source"] = "Toss Securities Open API"
    if args.command == "doctor":
        try:
            quote(client, ["005930"])
        except TmonError:
            result["data"]["remote"] = "실패"
            raise
        result["data"]["remote"] = "정상"
        return 0
    if args.command == "quote":
        data, meta, warnings, code = quote(client, args.symbols)
    elif args.command == "rank":
        data, meta, warnings, code = rank(client, args.market, args.by, args.duration, args.source,
                                         args.limit, args.exclude_caution)
    else:
        count = args.count if args.command == "history" else 120
        adjusted = not args.unadjusted if args.command == "history" else True
        data, meta, warnings, code = history(client, args.symbol, count, adjusted)
        if args.command == "analyze":
            if code:
                result["status"] = "partial"
            code = 0  # 120 is a warm-up target, not a required history size.
            data = analyze(data)
            if data["unavailable"]:
                warnings.append({"code": "unavailable-indicators", "message": "일부 지표는 데이터 부족 또는 0분모로 계산할 수 없습니다."})
                result["status"] = "partial"
            if all(value is None for value in data["indicators"].values()):
                code = 5
    result["data"] = data
    result["meta"].update(meta)
    result["warnings"] = warnings
    if code:
        result["status"] = "partial"
    return code


def table(headers, rows):
    def width(value):
        return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(value))
    rows = [["N/A" if cell is None else str(cell) for cell in row] for row in rows]
    widths = [max([width(header)] + [width(row[i]) for row in rows]) for i, header in enumerate(headers)]
    for row in [headers] + rows:
        print("  ".join(str(value) + " " * (size - width(value)) for value, size in zip(row, widths)))


def render(result, json_mode):
    # Same quantization for both output formats.
    result = json.loads(json.dumps(result, default=serialize, ensure_ascii=False))
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    data, meta = result["data"], result["meta"]
    if result["error"]:
        error = result["error"]
        print("오류 [%s]: %s" % (error["code"], error["message"]), file=sys.stderr)
        if error["requestId"]:
            print("요청 ID: " + error["requestId"], file=sys.stderr)
        if result["command"] == "doctor" and "checks" in meta:
            for name, status in meta["checks"].items():
                print("%s: %s" % (name, status), file=sys.stderr)
    elif result["command"] == "doctor":
        table(["CHECK", "STATUS"], list(data.items()))
    elif result["command"] == "quote":
        table(["SYMBOL", "PRICE", "CCY", "AS OF"], [[r[k] for k in ("symbol", "lastPrice", "currency", "timestamp")] for r in data])
    elif result["command"] == "recommend":
        from .recommend_render import render_recommend
        render_recommend(data, meta, table)
    elif result["command"] == "profile":
        table(["PROFILE", "SYMBOLS", "ACTIVE", "UPDATED AT"],
              [[row["name"], row["symbolCount"], "*" if row["active"] else "", row["updatedAt"]] for row in data])
        print("현재 선택: " + meta["activeProfile"] + " (모든 터미널에 공유)")
        print("저장 파일: " + meta["watchlistFile"])
    elif result["command"] == "watchlist":
        print("프로필: " + meta["profile"] + " · 현재 선택: " + meta["activeProfile"])
        if meta["action"] == "quote" and data:
            table(["SYMBOL", "PRICE", "CCY", "AS OF"], [[row[k] for k in ("symbol", "lastPrice", "currency", "timestamp")] for row in data])
        elif data:
            table(["#", "SYMBOL"], [[i, row["symbol"]] for i, row in enumerate(data, 1)])
        else:
            print("관심종목이 비어 있습니다. tmon watchlist add SYMBOL로 추가하세요.")
        for key, label in (("addedSymbols", "추가"), ("alreadyPresentSymbols", "이미 등록됨"),
                           ("removedSymbols", "삭제"), ("notFoundSymbols", "등록되지 않음")):
            if meta.get(key):
                print(label + ": " + ", ".join(meta[key]))
        print("저장된 관심종목: %d개" % meta["savedCount"])
        if meta["action"] == "quote":
            print("현재가 응답: %d개" % meta["returnedCount"])
        print("저장 파일: " + meta["watchlistFile"])
        print("목록 갱신 시각: " + (meta["updatedAt"] or "저장 기록 없음"))
    elif result["command"] == "search":
        if data:
            table(["SYMBOL", "NAME", "MARKET", "TYPE"], [[r[k] for k in ("symbol", "name", "market", "securityType")] for r in data])
        else:
            print("검색 결과가 없습니다.")
        print("%d개 일치 · %d개 표시%s" % (meta["totalMatches"], meta["returnedCount"], " (출력 제한)" if meta["truncated"] else ""))
        for cache in meta["catalogue"]:
            print("%s 목록: %s · %s" % (cache["market"], cache["fetchedAt"], "만료 캐시" if cache["stale"] else "캐시" if cache["cacheHit"] else "새로 조회"))
    elif result["command"] == "rank":
        scope = "시장 전체" if meta["aggregationScope"] == "market" else "토스증권 체결"
        basis = "기간 시작 대비" if meta["changeBasis"] == "period-start" else "전일 대비"
        print("%s · %s · %s · %s · 투자 유의 종목 %s" % (meta["market"], METRIC_LABELS[meta["by"]],
              meta["duration"], scope, "제외" if meta["excludeInvestmentCaution"] else "포함"))
        print("등락률: %s (%%) · 거래량·거래대금: 선택 기간 누적" % basis)
        print("랭킹 집계 시각: " + (meta["rankedAt"] or "미제공"))
        if data:
            table(["RANK", "SYMBOL", "PRICE", "PRICE CCY", "CHANGE %", "VOLUME", "AMOUNT (API)", "AMOUNT CCY"],
                  [[row[k] for k in ("rank", "symbol", "lastPrice", "currency", "changePct", "tradingVolume", "tradingAmount", "tradingAmountCurrency")] for row in data])
        else:
            print("해당 조건으로 집계된 랭킹이 없습니다.")
        print("%d개 요청 · %d개 반환" % (meta["requestedCount"], meta["returnedCount"]))
    elif result["command"] == "history":
        print("%s · %s · 수정주가 %s · %s ~ %s · %d봉" % (meta["symbol"], meta["currency"],
              "적용" if meta["adjusted"] else "미적용", meta["fromDate"], meta["toDate"], meta["usedCount"]))
        table(["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"],
              [[r[k] for k in ("date", "openPrice", "highPrice", "lowPrice", "closePrice", "volume")] for r in data])
    elif result["command"] == "analyze":
        print("%s · %s · 수정주가 적용 · %s ~ %s · %d봉" % (meta["symbol"], meta["currency"], meta["fromDate"], meta["toDate"], meta["usedCount"]))
        table(["METRIC", "VALUE"], [("lastClose", data["lastClose"]), ("lastVolume", data["lastVolume"])] + list(data["indicators"].items()))
        print("Pct: % 단위 · Ratio: 배수")
        for fact in data["facts"]:
            print(fact)
        for name, reason in data["unavailable"].items():
            print("%s: %s" % (name, reason), file=sys.stderr)
    if not result["error"]:
        if "migration" in meta:
            print("관심종목 저장 형식을 v2로 이전했습니다. 백업: " + meta["migration"]["backupFile"])
        print("조회 시각: " + meta["queriedAt"])
    for warning in result["warnings"]:
        print("안내 [%s]: %s" % (warning["code"], warning["message"]), file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = next((arg for arg in argv if arg in ("doctor", "quote", "history", "analyze", "search", "rank", "watchlist", "profile", "recommend")), None)
    result = envelope(command)
    try:
        args = parser().parse_args(argv)
        code = run(args, result)
    except TmonError as error:
        result.update(status="error", data=None, error=error.as_dict())
        code = error.exit_code
    except KeyboardInterrupt:
        result.update(status="error", data=None, error=TmonError("interrupted", "사용자가 중단했습니다.").as_dict())
        code = 130
    except Exception:
        result.update(status="error", data=None, error=TmonError("internal-error", "예상하지 못한 내부 오류입니다.", 1).as_dict())
        code = 1
    try:
        render(result, "--json" in argv)
    except BrokenPipeError:
        return 0
    return code
