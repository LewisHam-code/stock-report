"""Continue an explicitly enabled temporary queue inside the existing worker.

No extra service or timer. Normal research retains its configured cadence;
annual-only slices fill idle gaps and use their independent task budget.
"""
import time
from . import annual_batch, core

def selected_job():
    from . import current_batch
    return current_batch if current_batch.continuous() else annual_batch

def continuous():
    job=selected_job();task=job.load()
    return bool(task.get('continuous_idle') and job.active(task)
                and task.get('phase') in ('ready','running'))


def scheduled():
    from .pipeline import run
    from . import current_batch
    if not core.config().get('automatic_enabled',False):
        return {'ok':True,'state':'disabled'}
    if current_batch.managed() and current_batch.active():
        return _current_scheduled(current_batch)
    started=time.monotonic()
    result=run()
    while continuous():
        job=selected_job()
        cfg=core.config()
        if not cfg.get('automatic_enabled',False):break
        interval=cfg.get('interval_minutes',60)
        if job.__name__.endswith('current_batch') and cfg.get('schedule_policy')=='adaptive-v1':
            # Discovery and prices have their own service lanes; do not redo
            # every rendered report at each 15-minute discovery boundary.
            interval=max(interval,cfg.get('quarter_housekeeping_minutes',interval))
        due=started+interval*60
        if time.monotonic()>=due:
            # Re-enter the original workflow at its own cadence. It publishes
            # ordinary results before doing another annual-report slice.
            started=time.monotonic()
            result=run()
            if result.get('code')=='already_running':break
            continue
        before=job.load().get('cursor')
        try:
            with core.run_lock():
                outcome=job.tick(max_seconds=min(300,max(1,due-time.monotonic())))
        except core.ServiceError as error:
            if error.code=='already_running':break
            raise
        result={**result,'current_batch' if job.__name__.endswith('current_batch') else 'annual_batch':outcome}
        if outcome.get('phase')!='ready':break
        if job.load().get('cursor')==before:
            # A ready slice must advance. Never spin or buy repeated requests
            # if an unexpected path returns without durable progress.
            with job.edit() as task:
                task.update(enabled=False,phase='blocked',blocker='annual_no_progress')
            result['current_batch' if job.__name__.endswith('current_batch') else 'annual_batch']=job.status()
            break
    return result


def _current_scheduled(job):
    """New paid work precedes legacy housekeeping; each report is still checked.

    Discovery/downloads/prices run in the existing service lanes. Receipts and
    the queue's bounded archive policy remain owned by tick().
    """
    result={'ok':True,'state':'current_queue_priority'}
    while core.config().get('automatic_enabled',False) and job.active():
        if not job.continuous():
            result['current_batch']=job.status();break
        before=job.load().get('cursor')
        try:
            with core.run_lock():
                outcome=job.tick(max_seconds=300)
        except core.ServiceError as error:
            if error.code=='already_running':return {'ok':False,'code':'already_running'}
            raise
        result['current_batch']=outcome
        if outcome.get('phase')!='ready':break
        if job.load().get('cursor')==before:
            with job.edit() as task:
                task.update(enabled=False,phase='blocked',blocker='annual_no_progress')
            result['current_batch']=job.status();break
    return result
