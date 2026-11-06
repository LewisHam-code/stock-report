"""Model-free discovery/download lane, independent of the research run lock."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from contextlib import contextmanager
from datetime import timedelta
import fcntl
import json
import time
from . import core,job_schedule

def home():return core.VAULT/'00_基础资料'/'财报采集调度'
def read(path):
    try:return json.loads(path.read_text()) if path.is_file() else {}
    except (ValueError,OSError):return {}

def state():return read(home()/'状态.json')

def _download(row):
    from . import current_batch
    from .sources import archive_pdf,extract_pdf
    store=core.Store()
    try:
        if current_batch.managed():doc=current_batch.prepare(store,row)
        else:
            doc=dict(row);pdf,sha,_=archive_pdf(doc,store);doc.update(pdf=pdf,sha256=sha)
            pdf,sha,text,pages=extract_pdf(doc);doc.update(pdf=pdf,sha256=sha,text_file=text,page_count=pages)
        store.add_document(doc)
        store.execute("UPDATE documents SET pdf=?,sha256=?,text_file=?,page_count=?,state='parsed',error_code=NULL,next_retry=NULL WHERE id=? AND state!='complete'",
                      (doc['pdf'],doc['sha256'],doc['text_file'],doc['page_count'],doc['id']))
        if current_batch.managed():
            with current_batch.edit() as task:task['downloaded_ids']=sorted(set(task.get('downloaded_ids',[]))|{row['id']})
        return {'id':row['id'],'state':'parsed'}
    except core.ServiceError as error:
        attempts=row.get('attempts',0)+1
        review=error.code in ('ocr_required','pdf_changed','financial_proof_pending','correction_unresolved') or attempts>=3
        store.execute("UPDATE documents SET attempts=?,state=?,error_code=?,next_retry=? WHERE id=? AND state!='complete'",
                      (attempts,'review' if review else 'pending',error.code,None if review else (core.now()+timedelta(minutes=30)).isoformat(),row['id']))
        return {'id':row['id'],'state':'review' if review else 'pending','error':error.code}
    except Exception:
        store.execute("UPDATE documents SET state='review',error_code='pdf_parse_failed' WHERE id=? AND state!='complete'",(row['id'],))
        return {'id':row['id'],'state':'review','error':'pdf_parse_failed'}
    finally:store.conn.close()

def run(force=False):
    from . import current_batch
    from .sources import collect
    from .watchlist import watched_codes
    from .pipeline import retire_delisted
    started=time.monotonic();at=core.now();base=home();base.mkdir(parents=True,exist_ok=True)
    with (base/'采集.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'busy','new':0,'parsed':0}
        previous=state()
        if not force and not job_schedule.disclosure_due(previous,at):return {'state':'not_due','new':0,'parsed':0}
        value={'state':'running','last_started_at':at.isoformat(),**job_schedule.disclosure_slot(at),'new':0,'parsed':0,'errors':[]}
        core.atomic_json(base/'状态.json',value)
        store=core.Store()
        try:
            cfg=core.config();priority=watched_codes(cfg)
            from .securities import refresh
            refresh(force=False);scan=collect(store,priority);retire_delisted(store)
            current_batch.enqueue_discovered(store)
            value.update(state='complete',new=scan['new'],sources=scan['sources'],pages=scan.get('pages'),examined=scan.get('examined'),duration_seconds=round(time.monotonic()-started,2),last_completed_at=core.stamp())
            # A local first-seen clock is not the official publication time.
            seen=store.rows('SELECT id,market,published_at,created_at FROM documents WHERE created_at>=?',(at.isoformat(timespec='seconds'),))
            core.atomic_json(base/'发现记录'/(at.strftime('%Y%m%d-%H%M%S')+'.json'),
                             {'checked_at':at.isoformat(),'mode':value['mode'],'new':len(seen),'documents':[
                                 {'id':r['id'],'market':r['market'],'disclosed_at':r['published_at'],
                                  'publication_time_precision':'date' if 'T00:00:00' in r['published_at'] or len(r['published_at'])==10 else 'timestamp',
                                  'first_seen_at':r['created_at']} for r in seen],
                              'observation_days':14,'auto_reduce_hours':False})
            core.atomic_json(base/'状态.json',value)
            return value
        except Exception:
            value.update(state='failed',error='disclosure_intake_failed',last_completed_at=core.stamp())
            core.atomic_json(base/'状态.json',value)
            return value
        finally:store.conn.close()


def download_pending():
    from . import current_batch
    from .watchlist import watched_codes
    base=home();base.mkdir(parents=True,exist_ok=True)
    with (base/'下载.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'busy','parsed':0}
        store=core.Store();priority=watched_codes(core.config())
        value={'state':'running','last_started_at':core.stamp(),'parsed':0,'errors':[]}
        core.atomic_json(base/'下载状态.json',value)
        try:
            if current_batch.managed():
                queue=read(current_batch.folder()/'队列.json')
                task=current_batch.load();states=task.get('states',{});inflight=task.get('inflight',{})
                candidates=[]
                for row in queue:
                    if states.get(row['id']) in ('done','failed','needs_review','superseded') or row['id'] in inflight:continue
                    doc=store.rows('SELECT * FROM documents WHERE id=?',(row['id'],))
                    saved=doc[0] if doc else row
                    if saved.get('pdf') and saved.get('text_file'):continue
                    if saved.get('state')=='review' or saved.get('next_retry') and saved['next_retry']>core.stamp():continue
                    if current_batch.eligible(row):candidates.append({**saved,**row})
            else:
                from .pipeline import download_queue
                candidates=download_queue(store,-1,priority)
            # Bounded concurrency is a resource limit, never a daily/file cap.
            with ThreadPoolExecutor(max_workers=3) as pool:
                jobs=[pool.submit(_download,row) for row in candidates]
                for job in as_completed(jobs):
                    result=job.result()
                    if result['state']=='parsed':value['parsed']+=1
                    elif result.get('error'):value['errors'].append({'document':result['id'],'code':result['error']})
            value.update(state='complete',last_completed_at=core.stamp())
            core.atomic_json(base/'下载状态.json',value)
            return value
        except Exception:
            value.update(state='failed',error='download_lane_failed',last_completed_at=core.stamp())
            core.atomic_json(base/'下载状态.json',value)
            return value
        finally:store.conn.close()
