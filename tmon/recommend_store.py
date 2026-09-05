"""Private run records and bounded news cache, separate from watchlists."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from decimal import Decimal

from .errors import TmonError


def encode(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError()


def root_directory():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/tmon/recommend'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'tmon/recommend'


def private_dir(path):
    path = Path(path).absolute()
    # Do not traverse symlinked storage directories.
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise OSError('symlink storage')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise OSError('invalid storage')
    path.chmod(0o700)
    return path


def write_json(path, value):
    with path.open('x', encoding='utf8') as f:
        path.chmod(0o600)
        json.dump(value, f, ensure_ascii=False, default=encode, allow_nan=False, indent=2)
        f.flush()
        os.fsync(f.fileno())


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else root_directory()

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
            write_json(temp / 'research.json', {r['symbol']: r['research'] for r in result['data'] or []})
            os.rename(temp, destination)
        finally:
            if temp.exists():
                shutil.rmtree(temp)

    def cache_read(self, key):
        try:
            path = self.root / 'cache' / (key + '.json')
            if any(p.is_symlink() for p in [path, *path.parents]):
                return None
            with path.open('rb') as f:
                raw = f.read(1000001)
            return json.loads(raw) if len(raw) <= 1000000 else None
        except (OSError, ValueError):
            return None

    def cache_write(self, key, value):
        folder = private_dir(self.root / 'cache')
        fd, name = tempfile.mkstemp(dir=folder, prefix='.cache-')
        os.close(fd)
        temp = Path(name)
        try:
            with temp.open('w', encoding='utf8') as f:
                json.dump(value, f, ensure_ascii=False, default=encode, allow_nan=False)
            os.replace(temp, folder / (key + '.json'))
        finally:
            if temp.exists():
                temp.unlink()
