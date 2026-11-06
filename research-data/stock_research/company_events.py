"""Append-only company event evidence. Metadata never becomes an investment fact."""
from __future__ import annotations

import hashlib
import fcntl
import json
import re
from urllib.parse import urlparse

from .core import VAULT, dumps, stamp


def append_events(code, market, events, vault=None):
    if not events or not re.fullmatch(r'\d{6}', str(code)) or market not in ('SH','SZ','BJ'):
        return 0
    path = (vault or VAULT) / '30_公司' / market / code / '事件' / '事件记录.jsonl'
    additions = []
    for event in events:
        if event.get('code') != code:
            continue
        url = event.get('source_url') or event.get('source')
        if urlparse(str(url)).scheme not in ('http','https'):
            continue
        category = event.get('category') or 'security_status'
        identity = hashlib.sha256(dumps([code, market, category, url]).encode()).hexdigest()
        fact = {'code':code, 'market':market, 'category':category, 'source_url':url,
                'provider':event.get('provider'), 'title':event.get('title'),
                'published_at':event.get('published_at'), 'effective_at':event.get('effective_at'),
                'occurrence_at':event.get('detected_at') if event.get('changes') else None,
                'changes':event.get('changes'),
                'verification_status':event.get('verification_status','official_catalog_change')}
        revision = hashlib.sha256(dumps([identity,fact]).encode()).hexdigest()
        additions.append({**fact,'event_id':identity,'revision_id':revision,
                          'detected_at':event.get('detected_at') or event.get('captured_at') or stamp()})
    if additions:
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a+',encoding='utf-8') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            stream.seek(0)
            seen = set()
            for line in stream:
                try:
                    seen.add(json.loads(line)['revision_id'])
                except (ValueError, KeyError, TypeError):
                    raise ValueError('公司事件记录损坏，停止追加并保留原文件') from None
            count = 0
            for item in additions:
                if item['revision_id'] not in seen:
                    stream.write(dumps(item)+'\n')
                    seen.add(item['revision_id'])
                    count += 1
            return count
    return 0
