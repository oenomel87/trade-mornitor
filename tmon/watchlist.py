"""Profile-based local watchlists, separate from disposable API caches."""

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import unicodedata

from .auth import secure_directory, secure_open
from .errors import TmonError
from .market import quote, symbols, timestamp

MAX_BYTES = 1024 * 1024


def config_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tmon"
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return base / "tmon"


def validated_symbols(values):
    if any(not isinstance(value, str) or len(value) > 64 for value in values):
        raise TmonError("invalid-symbol", "종목코드는 1~64자의 영문·숫자·점·하이픈으로 입력하세요.", 2)
    return symbols(values)


def profile_name(value):
    if not isinstance(value, str):
        raise TmonError("invalid-profile-name", "프로필 이름을 입력하세요.", 2)
    value = unicodedata.normalize("NFC", value)
    if not re.fullmatch(r"[가-힣A-Za-z0-9_-]{1,32}", value):
        raise TmonError("invalid-profile-name", "프로필 이름은 한글·영문·숫자·하이픈·밑줄 1~32자로 입력하세요.", 2)
    return value


def file_error():
    return TmonError("invalid-watchlist", "관심종목 파일의 형식·버전이 올바르지 않습니다. 원본을 보존했으니 파일을 확인하세요.", 2)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def empty_state():
    return {"version": 2, "activeProfile": "default",
            "profiles": {"default": {"symbols": [], "updatedAt": None}}, "updatedAt": None}


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(data):
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise TmonError("watchlist-too-large", "관심종목 저장 파일은 최대 1 MiB입니다. 이번 변경은 저장하지 않았습니다.", 2)
    return raw


class Watchlist:
    def __init__(self, directory=None, lock_timeout=5):
        self.directory = Path(directory) if directory is not None else config_directory()
        self.path = self.directory / "watchlist.json"
        self.lock_timeout = lock_timeout

    def _read(self):
        if self.directory.exists() or self.directory.is_symlink():
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("unsafe watchlist directory")
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return empty_state(), None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError("unsafe watchlist file")
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise TmonError("watchlist-too-large", "관심종목 저장 파일이 1 MiB 한도를 초과합니다. 원본을 보존했습니다.", 2)
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
            if not isinstance(data, dict) or type(data.get("version")) is not int:
                raise file_error()
            legacy = data["version"] == 1
            if legacy:
                self._validate_list(data, nullable=False)
                data = {"version": 2, "activeProfile": "default", "updatedAt": data["updatedAt"],
                        "profiles": {"default": {"symbols": data["symbols"], "updatedAt": data["updatedAt"]}}}
            elif data["version"] == 2:
                profiles = data.get("profiles")
                if not isinstance(profiles, dict) or "default" not in profiles:
                    raise file_error()
                active = profile_name(data.get("activeProfile"))
                if active != data["activeProfile"] or active not in profiles:
                    raise file_error()
                timestamp(data.get("updatedAt"))
                for name, saved in profiles.items():
                    if profile_name(name) != name:
                        raise file_error()
                    self._validate_list(saved, nullable=(name == "default"))
            else:
                raise file_error()
            return data, raw if legacy else None
        except (ValueError, UnicodeError, TmonError, RecursionError):
            raise file_error() from None

    @staticmethod
    def _validate_list(data, nullable=False):
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list) or len(data["symbols"]) > 200:
            raise file_error()
        saved = data["symbols"]
        if saved and validated_symbols(saved) != saved:
            raise file_error()
        if "updatedAt" not in data:
            raise file_error()
        if data["updatedAt"] is None and nullable and not saved:
            return
        timestamp(data["updatedAt"])

    @contextmanager
    def locked(self):
        secure_directory(self.directory)
        fd = secure_open(self.directory / "watchlist.lock", os.O_CREAT | os.O_RDWR)
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TmonError("watchlist-busy", "다른 작업이 관심종목을 변경 중입니다. 잠시 후 다시 실행하세요.", 2, True)
                    time.sleep(min(0.05, remaining))
            yield
        finally:
            os.close(fd)

    def _write(self, data):
        raw = encoded(data)
        fd, temporary = tempfile.mkstemp(prefix=".watchlist-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _backup(self, raw):
        fd, path = tempfile.mkstemp(prefix="watchlist.v1.", suffix=".bak", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            os.unlink(path)
            raise
        return path

    @contextmanager
    def state(self, changing=False):
        changes = {}
        try:
            with self.locked() if changing else nullcontext():
                data, legacy = self._read()
                before = json.dumps(data, ensure_ascii=False)
                yield data, changes
                if changing and json.dumps(data, ensure_ascii=False) != before:
                    data["updatedAt"] = now()
                    encoded(data)  # Reject oversized changes before making a migration backup.
                    if legacy is not None:
                        backup = self._backup(legacy)
                        changes["migration"] = {"fromVersion": 1, "toVersion": 2, "backupFile": backup}
                    self._write(data)
        except OSError:
            raise TmonError("watchlist-unavailable", "관심종목 저장 경로·파일 권한을 확인하세요.", 2) from None

    @staticmethod
    def require_profile(data, name):
        if name not in data["profiles"]:
            raise TmonError("profile-not-found", "프로필이 없습니다. tmon profile list로 확인하거나 profile create NAME으로 생성하세요.", 2)
        return data["profiles"][name]

    def apply(self, action="list", values=None, profile=None):
        if action not in ("list", "add", "remove", "quote"):
            raise TmonError("invalid-watchlist-action", "지원하지 않는 관심종목 작업입니다.", 2)
        explicit = profile_name(profile) if profile is not None else None
        requested = validated_symbols(values or []) if action in ("add", "remove") else []
        with self.state(action in ("add", "remove")) as (data, changes):
            selected = explicit if explicit is not None else data["activeProfile"]
            saved_profile = self.require_profile(data, selected)
            saved = saved_profile["symbols"]
            if action == "add":
                added = [s for s in requested if s not in saved]
                changes.update(addedSymbols=added, alreadyPresentSymbols=[s for s in requested if s in saved])
                updated = saved + added
                if len(updated) > 200:
                    raise TmonError("watchlist-full", "프로필마다 최대 200개입니다. 이번 추가 요청은 저장하지 않았습니다.", 2)
            elif action == "remove":
                changes.update(removedSymbols=[s for s in requested if s in saved],
                               notFoundSymbols=[s for s in requested if s not in saved])
                updated = [s for s in saved if s not in requested]
            else:
                updated = saved
            if updated != saved:
                saved_profile.update(symbols=updated, updatedAt=now())
        meta = {"action": action, "watchlistFile": str(self.path), "updatedAt": saved_profile["updatedAt"],
                "savedCount": len(updated), "profile": selected, "activeProfile": data["activeProfile"],
                "profileSource": "explicit" if explicit is not None else "active",
                "storeUpdatedAt": data["updatedAt"], **changes}
        return [{"symbol": symbol} for symbol in updated], meta, [], 0

    def profile(self, action="list", name=None, new_name=None, force=False):
        if action not in ("list", "create", "use", "rename", "delete"):
            raise TmonError("invalid-profile-action", "지원하지 않는 프로필 작업입니다.", 2)
        if action != "list":
            name = profile_name(name)
        if action == "rename":
            new_name = profile_name(new_name)
        with self.state(action != "list") as (data, changes):
            profiles = data["profiles"]
            if action == "create":
                if name in profiles:
                    raise TmonError("profile-exists", "같은 이름의 프로필이 이미 있습니다.", 2)
                profiles[name] = {"symbols": [], "updatedAt": now()}
                changes["createdProfile"] = name
            elif action != "list":
                saved = self.require_profile(data, name)
                if action == "use":
                    changes.update(previousProfile=data["activeProfile"], selectedProfile=name)
                    data["activeProfile"] = name
                elif action == "rename":
                    if name != new_name:
                        if name == "default":
                            raise TmonError("reserved-profile", "default 프로필은 이름 변경·삭제할 수 없습니다.", 2)
                        if new_name in profiles:
                            raise TmonError("profile-exists", "같은 이름의 프로필이 이미 있습니다.", 2)
                        profiles[new_name] = profiles.pop(name)
                        saved["updatedAt"] = now()
                        if data["activeProfile"] == name:
                            data["activeProfile"] = new_name
                    changes.update(previousName=name, newName=new_name)
                else:
                    if name == "default":
                        raise TmonError("reserved-profile", "default 프로필은 이름 변경·삭제할 수 없습니다.", 2)
                    if data["activeProfile"] == name:
                        raise TmonError("active-profile", "선택된 프로필입니다. profile use default 등으로 전환한 뒤 삭제하세요.", 2)
                    if saved["symbols"] and not force:
                        raise TmonError("profile-not-empty", "종목이 있는 프로필입니다. 목록까지 삭제하려면 --force를 지정하세요.", 2)
                    del profiles[name]
                    changes["deletedProfile"] = name
        rows = [{"name": name, "symbolCount": len(profiles[name]["symbols"]),
                 "active": name == data["activeProfile"], "updatedAt": profiles[name]["updatedAt"]}
                for name in sorted(profiles, key=lambda name: (name != "default", name))]
        meta = {"action": action, "watchlistFile": str(self.path), "activeProfile": data["activeProfile"],
                "updatedAt": data["updatedAt"], **changes}
        return rows, meta, [], 0


def run_watchlist(store, action, values, client_factory, profile=None, context=None):
    rows, meta, warnings, code = store.apply(action, values, profile)
    if context is not None:
        context.update(meta)  # Keep the resolved profile even when API lookup raises.
    if action != "quote":
        return rows, meta, warnings, code
    if not rows:
        meta.update(missingSymbols=[], requestedCount=0, returnedCount=0)
        return [], meta, [], 0
    requested = [row["symbol"] for row in rows]
    data, quote_meta, warnings, code = quote(client_factory(), requested)
    meta.update(quote_meta, requestedCount=len(requested), returnedCount=len(data))
    return data, meta, warnings, code
