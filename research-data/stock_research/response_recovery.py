"""Reuse complete saved provider extractions; never send a model request.

Original responses stay intact. Narrow syntax repairs and missing-page
candidates are audited, and the normal PDF verification still runs afterward.
"""
import copy
import hashlib
import json
import re
from . import core


def parse(text):
    try:return json.loads(text),[]
    except json.JSONDecodeError:pass
    changes=[]
    # A page range is presentation syntax, never arithmetic. Match only this
    # schema key outside a quoted string, not arbitrary numbers or text.
    def ranges(match):
        entries=[]
        for token in match.group(1).split(','):
            token=token.strip()
            if not re.fullmatch(r'\d+(?:\s*-\s*\d+)?',token):return match.group(0)
            bounds=[int(v) for v in token.split('-')]
            if not all(1<=v<=5000 for v in bounds) or len(bounds)==2 and not 0<=bounds[1]-bounds[0]<=100:return match.group(0)
            entries.extend(range(bounds[0],bounds[-1]+1))
        changes.append('source_pages范围展开为逐页列表')
        return '"source_pages":'+json.dumps(entries)
    # Tokenize quoted strings first so a textual example can never be edited.
    tokens=re.split(r'("(?:[^"\\]|\\.)*")',text)
    for i in range(1,len(tokens),2):
        if tokens[i]=='"source_pages"' and i+1<len(tokens):
            tail=tokens[i+1]
            match=re.match(r'\s*:\s*\[([\d\s,-]*-[\d\s,-]*)\]',tail)
            if match:
                fixed=ranges(match)
                if fixed!=match.group(0):tokens[i]='';tokens[i+1]=fixed+tail[match.end():]
    normalized=''.join(tokens)
    # strict=False only permits literal control characters inside JSON
    # strings; no values, quotes or missing fields are invented.
    result=json.loads(normalized,strict=False)
    if normalized==text:changes.append('JSON字符串中的原始控制字符按原内容读取')
    return result,changes


def extraction(doc,directory):
    from .analysis import EXTRACTION,validate
    from .model_inputs import relocate_unlocated_claims
    source=core.ROOT/str(doc.get('pdf') or '')
    if not doc.get('pdf') or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=doc.get('sha256'):return None
    for path in sorted(directory.glob('financial_extraction-*.json'),reverse=True):
        # A later replacement PDF cannot adopt a response from an older file.
        if source.stat().st_mtime>path.stat().st_mtime:continue
        try:
            raw=json.loads(path.read_text());choice=raw['choices'][0]
            if choice.get('finish_reason')!='stop' or not str(raw.get('model','')).startswith('deepseek-v4-pro'):continue
            data,repairs=parse(choice['message']['content'])
            data,moved=relocate_unlocated_claims(data);validate(data,EXTRACTION)
        except (KeyError,IndexError,TypeError,ValueError,core.ServiceError):continue
        audit={'source_response':str(path.relative_to(core.VAULT)),'source_sha256':doc['sha256'],
               'recovered_at':core.stamp(),'normalizations':repairs+moved,'new_extraction_model_calls':0,
               'request_body_available':False}
        core.atomic_json(path.with_name(path.stem+'-恢复记录.json'),audit)
        return copy.deepcopy(data),{'provider':'deepseek','model':'deepseek-v4-pro','response_model':raw.get('model'),
                'usage':raw.get('usage',{}),'test_mode':False,'saved_response_recovery':audit}
    return None
