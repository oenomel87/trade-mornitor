"""Bounded Codex research worker. SDK is never imported by ordinary CLI commands."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from .market import timestamp
from .errors import TmonError
from .recommend_store import encode
from .recommend_config import RESEARCH_EFFORT

PROMPT_VERSION = 'news-v1'


def unavailable(reason='unavailable'):
    return {'researchStatus': 'unavailable', 'reason': reason, 'summary': '', 'catalysts': [],
            'counterEvidence': [], 'upcomingEvents': [], 'sources': [], 'cacheHit': False}


def safe_env():
    # SDK's env option merges into the parent; isolate at the Python process boundary.
    names = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'CODEX_HOME', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
             'SYSTEMROOT', 'USER', 'LOGNAME')
    result = {k: os.environ[k] for k in names if k in os.environ}
    result['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    result['PYTHONIOENCODING'] = 'utf-8'
    return result


def text_ok(value, maximum=4000):
    return isinstance(value, str) and len(value) <= maximum and all(ord(x) >= 32 or x in '\n\t' for x in value)


def validate(raw, symbols, asof):
    if not isinstance(raw, list) or len(raw) > len(symbols):
        raise ValueError('research shape')
    output = {}
    for item in raw:
        allowed = {'symbol','researchStatus','summary','catalysts','counterEvidence','upcomingEvents','sources'}
        if not isinstance(item, dict) or set(item) != allowed or item['symbol'] not in symbols or item['symbol'] in output:
            raise ValueError('research symbol/fields')
        if item['researchStatus'] not in ('verified', 'no-relevant-source') or not text_ok(item['summary']):
            raise ValueError('research status')
        sources = item['sources']
        if not isinstance(sources, list) or len(sources) > 12:
            raise ValueError('sources')
        ids = set()
        for s in sources:
            if not isinstance(s, dict) or set(s) != {'id','url','title','publishedAt'}:
                raise ValueError('source schema')
            u = urlparse(s['url']) if isinstance(s['url'],str) else None
            if not u or u.scheme not in ('http','https') or not u.hostname or u.username or u.password or not text_ok(s['url'],2048):
                raise ValueError('source url')
            if not text_ok(s['id'],80) or not s['id'] or s['id'] in ids or not text_ok(s['title'],500):
                raise ValueError('source identity')
            ids.add(s['id'])
            if s['publishedAt'] is not None and timestamp(s['publishedAt']) > asof:
                raise ValueError('future publication')
        count = 0
        for key in ('catalysts','counterEvidence','upcomingEvents'):
            if not isinstance(item[key],list) or len(item[key]) > 8:
                raise ValueError('facts')
            for fact in item[key]:
                if not isinstance(fact,dict) or set(fact) != {'text','sourceIds','eventAt'} or not text_ok(fact['text']):
                    raise ValueError('fact schema')
                if not isinstance(fact['sourceIds'],list) or not fact['sourceIds'] or any(s not in ids for s in fact['sourceIds']):
                    raise ValueError('uncited fact')
                if fact['eventAt'] is not None:
                    timestamp(fact['eventAt'])
                count += 1
        if item['researchStatus'] == 'verified' and (not sources or not count):
            raise ValueError('empty verified result')
        if item['researchStatus'] == 'no-relevant-source' and count:
            raise ValueError('inconsistent result')
        output[item['symbol']] = {k:v for k,v in item.items() if k != 'symbol'}
    if set(output) != set(symbols):
        raise ValueError('missing research')
    return output


def research(candidates, horizon, config, seconds, store=None, asof=None):
    asof = asof or datetime.now(timezone.utc)
    symbols = [r['symbol'] for r in candidates]
    if config['research'] == 'off':
        return {s:{**unavailable(), 'researchStatus':'disabled'} for s in symbols}, {'status':'disabled'}
    if not symbols:
        return {}, {'status':'not-needed'}
    output, pending, keys = {}, [], {}
    ttl = 900 if horizon == 'day' else 3600
    for row in candidates:
        material = [row['symbol'],horizon,config['model'],RESEARCH_EFFORT,PROMPT_VERSION,asof.date().isoformat()]
        key = hashlib.sha256(json.dumps(material).encode()).hexdigest()
        keys[row['symbol']] = key
        cached = store.cache_read(key) if store else None
        try:
            if cached and 0 <= (asof - timestamp(cached['retrievedAt'])).total_seconds() < ttl:
                verified = validate([cached['item']], [row['symbol']], asof)
                output[row['symbol']] = {**verified[row['symbol']], 'cacheHit':True,
                                         'retrievedAt':cached['retrievedAt'], 'model':cached['model']}
                continue
        except (ValueError, KeyError, TypeError, TmonError):
            pass  # Corrupt/expired caches are optional.
        pending.append({k:row[k] for k in ('symbol','name','market')})
    metadata = {'status':'ok', 'promptVersion':PROMPT_VERSION, 'sdkVersion':'0.147.0',
                'reasoningEffort':RESEARCH_EFFORT}
    if not pending:
        return output, {**metadata, 'cacheHit':True}
    if seconds <= 0:
        return {**output, **{r['symbol']:unavailable('research-timeout') for r in pending}}, {'status':'unavailable'}
    payload = {'candidates':pending, 'horizon':horizon, 'asOf':asof.isoformat(), 'model':config['model']}
    try:
        with tempfile.TemporaryDirectory(prefix='tmon-research-') as folder:
            p = subprocess.Popen([sys.executable, '-m', 'tmon.research_worker'], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=folder,
                                 env=safe_env(), start_new_session=True, text=True, encoding='utf8')
            try:
                out, _ = p.communicate(json.dumps(payload, ensure_ascii=False), timeout=seconds)
            finally:
                # Kill the worker's process group, including SDK runtime on timeout/cancellation.
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
        if len(out) > 1000000 or p.returncode != 0:
            raise ValueError('worker failed')
        response = json.loads(out)
        if response.get('error'):
            metadata['reason'] = response['error']
            raise ValueError('worker unavailable')
        if response.get('webSearchCount',0) < 1:
            raise ValueError('web search was not performed')
        validated = validate(response['items'], [r['symbol'] for r in pending], asof)
        metadata.update(model=response['model'], webSearchCount=response['webSearchCount'], usage=response.get('usage'))
        retrieved = datetime.now(timezone.utc).isoformat()
        for raw in response['items']:
            sym = raw['symbol']
            output[sym] = {**validated[sym], 'cacheHit':False, 'retrievedAt':retrieved, 'model':response['model']}
            if store:
                try:
                    store.cache_write(keys[sym], {'retrievedAt':retrieved,'model':response['model'],'item':raw})
                except OSError:
                    metadata['cacheWriteFailed'] = True
    except (OSError, ValueError, TypeError, KeyError, TmonError, subprocess.TimeoutExpired) as error:
        reason = 'research-timeout' if isinstance(error,subprocess.TimeoutExpired) else metadata.get('reason','research-unavailable')
        metadata.update(status='unavailable', reason=reason)
        output.update({r['symbol']:unavailable(reason) for r in pending})
    return output, metadata
