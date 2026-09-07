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
        self._owned_destinations = set()

    def save(self, result, inputs):
        runs = private_dir(self.root / 'runs')
        destination = runs / result['meta']['runId']
        temp = Path(tempfile.mkdtemp(prefix='.pending-', dir=runs))
        try:
            result['meta']['recordPath'] = str(destination / 'result.json')
            from .cli import serialize_result
            displayed = serialize_result(result)
            write_json(temp / 'result.json', displayed)
            write_json(temp / 'inputs.json', inputs)
            write_json(temp / 'research.json', {r['symbol']: r['research'] for r in result['data'] or []})
            # A second save can be needed when the first serialization/write
            # crosses a result expiry boundary. Only a run saved by this Store
            # instance may be replaced. Existing records from another process
            # are left intact, and a failed swap restores this run's record.
            destination_key = str(destination.absolute())
            owned = destination_key in self._owned_destinations
            if (destination.exists() or destination.is_symlink()) and not owned:
                raise OSError('run destination already exists')
            backup = None
            if owned:
                if destination.is_symlink() or not destination.is_dir():
                    raise OSError('invalid existing run destination')
                backup = Path(tempfile.mkdtemp(prefix='.previous-', dir=runs))
                backup.rmdir()
                try:
                    os.rename(destination, backup)
                    os.rename(temp, destination)
                except OSError:
                    if not destination.exists() and backup.exists():
                        os.rename(backup, destination)
                    raise
                try:
                    shutil.rmtree(backup)
                except OSError:
                    # The replacement is already visible. Retain the prior
                    # complete run in its private backup when cleanup fails;
                    # rolling back here could destroy a valid new result.
                    pass
            else:
                os.rename(temp, destination)
            self._owned_destinations.add(destination_key)
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
