"""User-private token cache and cross-process issuance lock (macOS/Linux)."""

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from contextlib import contextmanager

from .errors import TmonError


def cache_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "tmon"
    configured = os.environ.get("XDG_CACHE_HOME")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".cache"
    return base / "tmon"


def credentials():
    values = [os.environ.get(key, "") for key in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")]
    if not all(value.strip() for value in values):
        raise TmonError("missing-credentials", "TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET 환경 변수를 설정하세요.", 2)
    return values


def secure_directory(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("unsafe cache directory")
    directory.chmod(0o700)


def secure_open(path, flags):
    fd = os.open(str(path), flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise OSError("unsafe cache file")
    os.fchmod(fd, 0o600)
    return fd


class Auth:
    def __init__(self, transport, directory=None):
        self.client_id, self.secret = credentials()
        self.transport = transport
        self.directory = Path(directory) if directory else cache_directory()
        key = hashlib.sha256(self.client_id.encode()).hexdigest()
        self.path = self.directory / (key + ".json")
        self.lock_path = self.directory / (key + ".lock")
        self.cached = None

    @contextmanager
    def locked(self):
        secure_directory(self.directory)
        fd = secure_open(self.lock_path, os.O_CREAT | os.O_RDWR)
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

    def read(self):
        try:
            fd = secure_open(self.path, os.O_RDONLY)
            with os.fdopen(fd) as handle:
                return json.load(handle)
        except (FileNotFoundError, ValueError):
            return None

    def valid(self, data, rejected):
        if not isinstance(data, dict):
            return False
        token, expiry = data.get("access_token"), data.get("expires_at")
        return (isinstance(token, str) and bool(token) and "\n" not in token and "\r" not in token and token != rejected and
                isinstance(expiry, (int, float)) and math.isfinite(expiry) and expiry > time.time() + 60)

    def token(self, rejected=None):
        if self.valid(self.cached, rejected):
            return self.cached["access_token"]
        try:
            with self.locked():
                data = self.read()
                if not self.valid(data, rejected):
                    started = time.time()
                    response = self.transport.request("POST", "/oauth2/token", form={
                        "grant_type": "client_credentials", "client_id": self.client_id,
                        "client_secret": self.secret})
                    token, expiry = response.get("access_token"), response.get("expires_in")
                    if (not isinstance(token, str) or not token or "\n" in token or "\r" in token or
                            not isinstance(response.get("token_type"), str) or
                            response["token_type"].lower() != "bearer" or
                            not isinstance(expiry, int) or expiry <= 60):
                        raise TmonError("invalid-token-response", "인증 서버의 토큰 응답을 확인할 수 없습니다.", 3)
                    data = {"access_token": token, "expires_at": started + expiry}
                    fd, temporary = tempfile.mkstemp(prefix=".token-", dir=self.directory)
                    try:
                        with os.fdopen(fd, "w") as handle:
                            json.dump(data, handle)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, self.path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                self.cached = data
                return data["access_token"]
        except OSError:
            raise TmonError("cache-unavailable", "사용자 전용 토큰 캐시를 읽거나 쓸 수 없습니다. 캐시 경로·권한을 확인하세요.", 2) from None


def local_checks():
    checks = {key: "설정됨" if os.environ.get(key, "").strip() else "누락"
              for key in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")}
    directory = cache_directory()
    try:
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            available = stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and os.access(directory, os.W_OK | os.X_OK)
        else:
            parent = directory.parent
            while not parent.exists():
                parent = parent.parent
            available = os.access(parent, os.W_OK | os.X_OK)
        checks["tokenCache"] = "정상" if available else "실패"
    except OSError:
        checks["tokenCache"] = "실패"
    checks["remote"] = "미확인"
    return checks
