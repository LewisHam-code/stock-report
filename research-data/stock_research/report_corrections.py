"""Official replacement reports and replayable, local errata overlays."""
import difflib
import gzip
import json
import re
from . import core,sources


def prepare(document):
    pdf,sha,text,pages=sources.extract_pdf(document)
    return {**document,'pdf':pdf,'sha256':sha,'text_file':text,'page_count':pages}


def patches(before,after):
    changes=[]
    for tag,a,b,c,d in difflib.SequenceMatcher(None,before,after,autojunk=False).get_opcodes():
        if tag!='equal':changes.append({'start':a,'end':b,'before':before[a:b],'after':after[c:d]})
    return changes


def replay(text,changes):
    for edit in reversed(changes):
        if text[edit['start']:edit['end']]!=edit['before']:
            raise core.ServiceError('correction_unresolved','原稿更正定位不一致，保留版本等待复核')
        text=text[:edit['start']]+edit['after']+text[edit['end']:]
    return text


def classify(notice,original_pages,corrected_pages):
    compact=lambda t:re.sub(r'\s+','',t)
    full=compact(notice)
    if any(word in full for word in ('重新编制','全面修订','原报告作废','撤回原报告','整体重述')):
        return 'official_replacement'
    local=bool(re.search(r'(?:其他|其余)内容不变',full))
    if not local or len(original_pages)!=len(corrected_pages):return 'official_replacement'
    changed=total=0
    for old,new in zip(original_pages,corrected_pages):
        a,b=compact(old['text']),compact(new['text']);total+=len(a)
        changed+=sum(max(j-i,l-k) for tag,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes() if tag!='equal')
    return 'original_with_errata' if changed/max(1,total)<=.03 else 'official_replacement'


def resolve(store,row):
    corrected=prepare(row)
    if not any(w in row['title'] for w in ('更正','修订','更新')):return corrected
    originals=row.get('superseded_versions') or []
    if not originals:
        corrected['correction_context']={'mode':'official_replacement','reason':'官方新版全文；未获得可核对的原稿，不擅自推断为局部勘误','effective_url':row['source_url']}
        return corrected
    original=prepare(sorted(originals,key=lambda r:(r['published_at'],r['id']))[-1])
    identity=sources.cninfo_identity(store,row['code'])
    data=sources.query_announcements(row['published_at'][:10],core.now().date().isoformat(),{'SH':'sse','SZ':'szse','BJ':'bj'}[row['market']],1,keyword='更正',stock=identity)
    if data.get('totalAnnouncement',0)>len(data.get('announcements',[])):
        raise core.ServiceError('correction_unresolved','更正公告结果不完整，保留版本等待核对')
    year=row['period'][:4];names={'Q3':('第三季度','三季度'),'Q1':('第一季度','一季度'),'H1':('半年度','中期'),'FY':('年度',)}[row['period'][4:]]
    notices=[r for r in data['announcements'] if r.get('secCode')==row['code'] and year in r.get('announcementTitle','')
             and any(n in r['announcementTitle'] for n in names) and '更正公告' in r['announcementTitle']]
    if not notices:
        corrected['correction_context']={'mode':'official_replacement','reason':'未能确认仅局部勘误，采用可核对的官方更正全文','original_url':original['source_url'],'effective_url':corrected['source_url']}
        return corrected
    raw=max(notices,key=lambda r:int(r.get('announcementTime') or 0))
    notice=prepare({'id':str(raw['announcementId']),'code':row['code'],'company':row['company'],'market':row['market'],'period':row['period'],
                    'title':raw['announcementTitle'],'published_at':row['published_at'],'source_url':'https://static.cninfo.com.cn/'+raw['adjunctUrl']})
    a=json.loads((core.ROOT/original['text_file']).read_text());b=json.loads((core.ROOT/corrected['text_file']).read_text())
    notice_text='\n'.join(p['text'] for p in json.loads((core.ROOT/notice['text_file']).read_text())['pages'])
    mode=classify(notice_text,a['pages'],b['pages'])
    context={'mode':mode,'original_url':original['source_url'],'effective_url':corrected['source_url'],'notice_url':notice['source_url'],
             'notice_title':notice['title'],'original_sha256':original['sha256'],'effective_sha256':corrected['sha256'],
             'note':'原稿及官方更正版本均保留；模型只收到一份有效正文，不重复发送两个全文'}
    folder=core.VAULT/'30_公司'/row['market']/row['code']/'报告更正'
    record={**context,'created_at':core.stamp(),'period':row['period'],'page_patches':[]}
    if mode=='original_with_errata':
        effective=[]
        for old,new in zip(a['pages'],b['pages']):
            edits=patches(old['text'],new['text']);text=replay(old['text'],edits)
            if text!=new['text']:raise core.ServiceError('correction_unresolved','修正正文与官方更正稿不一致，未解读')
            effective.append({**new,'text':text})
            if edits:record['page_patches'].append({'page':old['page'],'edits':edits})
        derived={**b,'pages':effective,'text_parser':'official-correction-overlay-v1','correction_context':context}
        cache=core.ROOT/'text'/(corrected['id']+'-'+corrected['sha256'][:12]+'-effective.json')
        core.atomic_json(cache,derived);corrected['text_file']=str(cache.relative_to(core.ROOT))
        core.atomic_text(folder/(row['period']+'-'+row['id']+'-原稿修正后文本.json.gz.hex'),gzip.compress(core.dumps(derived).encode()).hex())
        context['note']='以原稿为底稿逐页应用可回放的更正，结果与官方更正全文逐字一致；财务事实仍核对官方有效PDF'
    record.update(context)
    core.atomic_json(folder/(row['period']+'-'+row['id']+'.json'),record)
    corrected['correction_context']=context
    return corrected
