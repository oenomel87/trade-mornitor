"""Versioned, explicit recommendation settings (no implicit env-file loading)."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from .errors import TmonError

DEFAULTS = {
    'configVersion': 1, 'capital': None, 'maxLossPct': None, 'research': 'auto', 'model': None,
    'universe': {'rankCount': 50, 'detailLimit': 30, 'researchLimit': 5},
    'day': {'minEstimatedAvgDailyAmount': '10000000000', 'maxSpreadPct': '0.30',
            'minVolumeRatio': '1.5', 'maxBreakoutGapPct': '1.0'},
    'swing': {'minEstimatedAvgDailyAmount': '10000000000', 'maxSpreadPct': '0.50',
              'minVolumeRatio': '1.5', 'maxBreakoutGapPct': '3.0'},
    'freshness': {'rankingSeconds': 120, 'quoteSeconds': 15, 'minuteSeconds': 120,
                  'stateSeconds': 30, 'resultSeconds': 30, 'barCompletionDelaySeconds': 5},
    'budget': {'totalSeconds': 180, 'dataSeconds': 80, 'researchSeconds': 70, 'finalSeconds': 30},
}


def bad():
    return TmonError('invalid-recommend-config', '추천 설정의 키·자료형·범위를 확인하세요.', 2)


def positive(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 40:
            raise ValueError()
        d = Decimal(str(value))
        if not d.is_finite() or d <= 0 or abs(d.adjusted()) > 20:
            raise ValueError()
        return d
    except (InvalidOperation, ValueError):
        raise bad() from None


def merge(base, update):
    if not isinstance(update, dict) or set(update) - set(base):
        raise bad()
    for k, v in update.items():
        if isinstance(base[k], dict):
            merge(base[k], v)
        else:
            base[k] = v


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise bad()
        result[key] = value
    return result


def load_config(path=None, *, capital=None, max_loss_pct=None, research=None, limit=3):
    c = deepcopy(DEFAULTS)
    if path:
        try:
            p = Path(path)
            with p.open('rb') as f:
                content = f.read(65537)
            if len(content) > 65536:
                raise bad()
            merge(c, json.loads(content, object_pairs_hook=unique_object))
        except (OSError, ValueError, UnicodeError):
            raise bad() from None
    for k, v in (('capital', capital), ('maxLossPct', max_loss_pct), ('research', research)):
        if v is not None:
            c[k] = v
    if type(c['configVersion']) is not int or c['configVersion'] != 1 or c['research'] not in ('auto', 'off'):
        raise bad()
    if c['model'] is not None and (not isinstance(c['model'], str) or not c['model'].strip() or len(c['model']) > 100):
        raise bad()
    for key in ('capital', 'maxLossPct'):
        if c[key] is not None:
            c[key] = str(positive(c[key]))
    if c['maxLossPct'] is not None and Decimal(c['maxLossPct']) >= 100:
        raise bad()
    for horizon in ('day', 'swing'):
        for k, v in c[horizon].items():
            c[horizon][k] = str(positive(v))
    for group in ('universe', 'freshness', 'budget'):
        for v in c[group].values():
            if type(v) is not int or not 1 <= v <= (100 if group == 'universe' else 3600):
                raise bad()
    u, b = c['universe'], c['budget']
    if not 1 <= u['researchLimit'] <= min(5, u['detailLimit']) or not 1 <= limit <= u['researchLimit']:
        raise bad()
    if sum(b[k] for k in ('dataSeconds', 'researchSeconds', 'finalSeconds')) > b['totalSeconds']:
        raise bad()
    return c


def config_hash(c):
    return hashlib.sha256(json.dumps(c, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
