"""Independent bounded lanes hosted by the existing loopback service."""
import os
import threading
from . import core,job_schedule


def start(server,name,target):
    previous=getattr(server,name,None)
    if previous and previous.is_alive():return False
    thread=threading.Thread(target=target,name=name,daemon=True)
    setattr(server,name,thread);thread.start();return True


def wake_worker():
    from .deployment import launch,WORKER
    # kickstart without -k never interrupts an in-flight worker.
    launch(['kickstart',f'gui/{os.getuid()}/{WORKER}'],required=False)


def poll(server,clock):
    cfg=core.config()
    if not cfg.get('automatic_enabled'):return
    from . import disclosure_intake as intake,tracking_market,current_batch
    if job_schedule.disclosure_due(intake.state()):
        start(server,'_disclosure_thread',intake.run)
    task=current_batch.load()
    managed=current_batch.managed()
    # A download attempt has its own source retry clock. One minute polling
    # permits newly discovered PDFs to enter the model lane promptly.
    if (not managed or current_batch.active(task)) and clock>=getattr(server,'_download_next',0):
        server._download_next=clock+60
        start(server,'_download_thread',intake.download_pending)
    market_state=tracking_market.read(tracking_market.home()/'控制.json')
    if job_schedule.market_due(market_state):
        start(server,'_tracking_thread',tracking_market.background_refresh)
    if clock<getattr(server,'_worker_next',0):return
    server._worker_next=clock+60
    task=current_batch.load()
    if managed:
        if not current_batch.active(task):return
        if task.get('phase')=='waiting_idle':
            from .model_timing import window
            if not window(current_batch.settings())['deferred']:
                with current_batch.edit() as value:value.update(phase='ready',blocker=None,resume_at=None)
        if not current_batch.continuous():return
    else:
        store=core.Store()
        try:
            if not store.rows("SELECT 1 FROM documents WHERE state='parsed' AND (next_retry IS NULL OR next_retry<=?) LIMIT 1",(core.stamp(),)):return
        finally:store.conn.close()
    try:
        with core.run_lock():pass
    except core.ServiceError:return
    start(server,'_wake_thread',wake_worker)
