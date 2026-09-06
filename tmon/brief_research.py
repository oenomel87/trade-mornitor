"""News-only SDK research; market observations are never cached with prose."""
from datetime import timedelta
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .errors import TmonError
from .market import timestamp
from .research import safe_env, text_ok
from .research_worker import object_schema
from .recommend_config import RESEARCH_MODEL, RESEARCH_EFFORT
from .brief_data import kst

VERSION = 'brief-news-v1'


def canonical_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError('source url')
    u = urlparse(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or not text_ok(value, 2048):
        raise ValueError('source url')
    query = [(k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urlunparse((u.scheme, u.netloc.lower(), u.path, '', urlencode(sorted(query)), ''))


def output_schema():
    string, nullable = {'type': 'string'}, {'type': ['string', 'null']}
    strings = {'type': 'array', 'items': string}
    fact = object_schema({'text': string, 'sourceIds': strings})
    news = object_schema({'headline': string, 'summary': string, 'impact': string,
                          'category': {'type': 'string', 'enum': ['domestic', 'global', 'watchlist']},
                          'symbols': strings, 'sourceIds': strings, 'eventAt': nullable})
    event = object_schema({'text': string, 'eventAt': nullable, 'sourceIds': strings})
    source = object_schema({'id': string, 'url': string, 'title': string, 'publishedAt': nullable})
    return object_schema({'status': {'type': 'string', 'enum': ['completed', 'no-relevant-source']},
                          'summary': {'type': 'array', 'items': fact},
                          'news': {'type': 'array', 'items': news},
                          'upcomingEvents': {'type': 'array', 'items': event},
                          'sources': {'type': 'array', 'items': source}})


def validate(raw, asof, requested_symbols=()):
    expected = {'status', 'summary', 'news', 'upcomingEvents', 'sources'}
    if not isinstance(raw, dict) or set(raw) != expected or raw['status'] not in ('completed', 'no-relevant-source'):
        raise ValueError('brief schema')
    for key, cap in (('summary', 3), ('news', 5), ('upcomingEvents', 5), ('sources', 15)):
        if not isinstance(raw[key], list) or len(raw[key]) > cap:
            raise ValueError('brief limit')
    # Copy before normalizing offsets so cached input remains immutable.
    raw = json.loads(json.dumps(raw))
    ids, urls = set(), set()
    for s in raw['sources']:
        if not isinstance(s, dict) or set(s) != {'id', 'url', 'title', 'publishedAt'}:
            raise ValueError('source fields')
        if not isinstance(s['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', s['id']) or s['id'] in ids or not text_ok(s['title'], 500) or not s['title'].strip():
            raise ValueError('source identity')
        url = canonical_url(s['url'])
        if url in urls:
            raise ValueError('duplicate source')
        ids.add(s['id'])
        urls.add(url)
        if s['publishedAt'] is not None:
            if timestamp(s['publishedAt']) > asof:
                raise ValueError('future publication')
            s['publishedAt'] = kst(s['publishedAt'])
    def refs(item):
        refs = item['sourceIds']
        if not isinstance(refs, list) or not refs or any(not isinstance(i, str) or i not in ids for i in refs):
            raise ValueError('uncited text')
    for item in raw['summary']:
        if not isinstance(item, dict) or set(item) != {'text', 'sourceIds'} or not text_ok(item['text'], 1200) or not item['text'].strip():
            raise ValueError('summary')
        refs(item)
    headlines = set()
    source_map = {s['id']: s for s in raw['sources']}
    for item in raw['news']:
        if not isinstance(item, dict) or set(item) != {'headline', 'summary', 'impact', 'category', 'symbols', 'sourceIds', 'eventAt'}:
            raise ValueError('news fields')
        for key, cap in (('headline', 300), ('summary', 2000), ('impact', 1500)):
            if not text_ok(item[key], cap) or (key != 'impact' and not item[key].strip()):
                raise ValueError('news text')
        if item['headline'] in headlines or item['category'] not in ('domestic', 'global', 'watchlist'):
            raise ValueError('news identity')
        headlines.add(item['headline'])
        if not isinstance(item['symbols'], list) or any(not isinstance(s, str) or s not in requested_symbols for s in item['symbols']):
            raise ValueError('watchlist symbol')
        if item['category'] == 'watchlist' and not item['symbols']:
            raise ValueError('unidentified watchlist news')
        refs(item)
        linked = [source_map[s] for s in item['sourceIds']]
        if all(s['publishedAt'] is not None and timestamp(s['publishedAt']) < asof - timedelta(hours=72) for s in linked):
            raise ValueError('old news presented as current')
        if item['eventAt'] is not None:
            if timestamp(item['eventAt']) > asof:
                raise ValueError('future news event; use upcomingEvents')
            item['eventAt'] = kst(item['eventAt'])
    for item in raw['upcomingEvents']:
        if not isinstance(item, dict) or set(item) != {'text', 'eventAt', 'sourceIds'} or not text_ok(item['text'], 1200) or not item['text'].strip():
            raise ValueError('upcoming event')
        refs(item)
        if item['eventAt'] is not None:
            if not asof <= timestamp(item['eventAt']) <= asof + timedelta(days=7):
                raise ValueError('event window')
            item['eventAt'] = kst(item['eventAt'])
    if raw['status'] == 'completed' and (not raw['sources'] or not raw['news']):
        raise ValueError('empty completed research')
    if raw['status'] == 'no-relevant-source' and any(raw[k] for k in expected - {'status'}):
        raise ValueError('inconsistent empty research')
    return raw


def empty(reason, status='unavailable'):
    return {'status': status, 'reason': reason, 'summary': [], 'news': [], 'upcomingEvents': [], 'sources': []}


def cache_key(payload):
    # Do not include live market figures; this cache contains external news only.
    content = [VERSION, RESEARCH_MODEL, RESEARCH_EFFORT, payload['session'], payload['context']['date'], payload['watchlist']]
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def research(payload, seconds, store, refresh=False):
    asof = timestamp(payload['asOf'])
    syms = [r['symbol'] for r in payload['watchlist']]
    key = cache_key(payload)
    ttl = 600 if payload['context']['phase'] == 'intraday' else 1800
    metadata = {'model': RESEARCH_MODEL, 'reasoningEffort': RESEARCH_EFFORT, 'sdkVersion': '0.147.0',
                'promptVersion': VERSION, 'cacheHit': False, 'cacheTtlSeconds': ttl}
    cached = None if refresh else store.cache_read(key)
    try:
        if (cached and timestamp(cached['asOf']) <= timestamp(cached['retrievedAt']) <= asof and
                0 <= (asof - timestamp(cached['retrievedAt'])).total_seconds() < ttl):
            validated = validate(cached['result'], timestamp(cached['asOf']), syms)
            return validated, {**metadata, 'cacheHit': True, 'asOf': cached['asOf'], 'retrievedAt': cached['retrievedAt']}
    except (ValueError, TypeError, KeyError, TmonError):
        pass
    if seconds <= 0:
        return empty('research-timeout'), metadata
    try:
        with tempfile.TemporaryDirectory(prefix='tmon-brief-') as folder:
            p = subprocess.Popen([sys.executable, '-m', 'tmon.brief_worker'], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=folder,
                                 env=safe_env(), start_new_session=True, text=True, encoding='utf8')
            try:
                out, _ = p.communicate(json.dumps(payload, ensure_ascii=False), timeout=seconds)
            finally:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
        if p.returncode != 0 or len(out) > 1000000:
            raise ValueError('worker output')
        response = json.loads(out)
        if response.get('error'):
            reason = response['error'] if response['error'] in ('sdk-not-installed', 'chatgpt-login-required', 'sdk-research-failed') else 'research-unavailable'
            return empty(reason), metadata
        if type(response.get('webSearchCount')) is not int or response['webSearchCount'] < 1:
            raise ValueError('no web search')
        result = validate(response['result'], asof, syms)
        from .intraday import utcnow
        retrieved = kst(utcnow().isoformat())
        metadata.update(asOf=payload['asOf'], retrievedAt=retrieved, webSearchCount=response['webSearchCount'], usage=response.get('usage'))
        try:
            store.cache_write(key, {'result': result, 'asOf': payload['asOf'], 'retrievedAt': retrieved})
        except OSError:
            metadata['cacheWriteFailed'] = True
        return result, metadata
    except (OSError, ValueError, TypeError, KeyError, TmonError, subprocess.TimeoutExpired) as e:
        return empty('research-timeout' if isinstance(e, subprocess.TimeoutExpired) else 'research-unavailable'), metadata
