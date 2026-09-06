"""Private atomic briefing records and per-profile overlap protection."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .auth import secure_open
from .errors import TmonError
from .recommend_store import Store, root_directory, private_dir, write_json


class BriefStore(Store):
    def __init__(self, root=None):
        super().__init__(root if root is not None else root_directory().parent / 'brief')

    @contextmanager
    def locked(self, scope):
        folder = private_dir(self.root / 'locks')
        fd = secure_open(folder / (scope + '.lock'), os.O_CREAT | os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TmonError('brief-already-running', '같은 프로필의 브리핑이 실행 중입니다. 완료 후 다시 실행하세요.', 2) from None
            yield
        finally:
            os.close(fd)

    def save(self, result, inputs):
        runs = private_dir(self.root / 'runs')
        destination = runs / result['meta']['runId']
        temp = Path(tempfile.mkdtemp(prefix='.pending-', dir=runs))
        try:
            result['meta']['recordPath'] = str(destination / 'result.json')
            from .cli import serialize
            displayed = json.loads(json.dumps(result, default=serialize, ensure_ascii=False, allow_nan=False))
            write_json(temp / 'result.json', displayed)
            write_json(temp / 'inputs.json', inputs)
            write_json(temp / 'research.json', displayed['data']['research'])
            os.rename(temp, destination)
        except OSError:
            result['meta'].pop('recordPath', None)
            raise
        finally:
            if temp.exists():
                shutil.rmtree(temp)


def scope_key(profile, symbols):
    return hashlib.sha256(json.dumps(['brief-v1', profile, symbols], ensure_ascii=False).encode()).hexdigest()
