import unittest
from contextlib import nullcontext
from unittest.mock import patch
from stock_research import annual_runner as runner,annual_batch,core,pipeline


class AnnualRunnerTests(unittest.TestCase):
    def setUp(self):
        managed=patch('stock_research.current_batch.managed',return_value=False)
        managed.start();self.addCleanup(managed.stop)
        selected=patch.object(runner,'selected_job',return_value=annual_batch)
        selected.start();self.addCleanup(selected.stop)
    def test_normal_schedule_does_not_repeat_when_temporary_mode_is_absent(self):
        with patch.object(core,'config',return_value={'automatic_enabled':True}),patch.object(runner,'continuous',return_value=False),patch.object(pipeline,'run',return_value={'state':'ok'}) as run:
            self.assertEqual(runner.scheduled(),{'state':'ok'})
        run.assert_called_once()

    def test_continuous_queue_starts_next_slice_without_sleep_or_daily_pipeline(self):
        with patch.object(core,'config',return_value={'automatic_enabled':True,'interval_minutes':60}),patch.object(runner,'continuous',side_effect=[True,True]),patch.object(runner.time,'monotonic',return_value=0),patch.object(core,'run_lock',return_value=nullcontext()),patch.object(annual_batch,'load',side_effect=[{'cursor':1},{'cursor':2},{'cursor':2}]),patch.object(annual_batch,'tick',side_effect=[{'phase':'ready'},{'phase':'waiting_idle'}]) as tick,patch.object(pipeline,'run',return_value={}) as run:
            result=runner.scheduled()
        self.assertEqual(tick.call_count,2);run.assert_called_once()
        self.assertEqual(result['annual_batch']['phase'],'waiting_idle')
        self.assertEqual(tick.call_args_list[0].kwargs['max_seconds'],300)

    def test_original_daily_work_runs_again_when_its_interval_is_due(self):
        with patch.object(core,'config',return_value={'automatic_enabled':True,'interval_minutes':60}),patch.object(runner,'continuous',side_effect=[True,False]),patch.object(runner.time,'monotonic',side_effect=[0,3601,3601]),patch.object(pipeline,'run',return_value={'state':'ok'}) as run,patch.object(annual_batch,'tick') as tick:
            runner.scheduled()
        self.assertEqual(run.call_count,2);tick.assert_not_called()

    def test_cost_or_balance_hold_ends_continuation(self):
        for phase in ('cost_approval_wait','balance_stopped','complete','disabled'):
            with self.subTest(phase=phase),patch.object(core,'config',return_value={'automatic_enabled':True,'interval_minutes':60}),patch.object(runner,'continuous',return_value=True),patch.object(runner.time,'monotonic',return_value=0),patch.object(core,'run_lock',return_value=nullcontext()),patch.object(annual_batch,'load',return_value={'cursor':1}),patch.object(annual_batch,'tick',return_value={'phase':phase}) as tick,patch.object(pipeline,'run',return_value={}):
                self.assertEqual(runner.scheduled()['annual_batch']['phase'],phase)
                tick.assert_called_once()

    def test_archived_or_waiting_task_cannot_continue(self):
        for phase,enabled in [('archived',False),('disabled',False),('waiting_idle',True)]:
            with self.subTest(phase=phase),patch.object(annual_batch,'load',return_value={'continuous_idle':True,'enabled':enabled,'phase':phase}),patch.object(annual_batch,'active',return_value=enabled):
                self.assertFalse(runner.continuous())

    def test_no_progress_blocks_instead_of_repeating_paid_work(self):
        task={}
        with patch.object(core,'config',return_value={'automatic_enabled':True,'interval_minutes':60}),patch.object(runner,'continuous',return_value=True),patch.object(runner.time,'monotonic',return_value=0),patch.object(core,'run_lock',return_value=nullcontext()),patch.object(annual_batch,'load',return_value={'cursor':1}),patch.object(annual_batch,'edit',return_value=nullcontext(task)),patch.object(annual_batch,'status',return_value={'phase':'blocked'}),patch.object(annual_batch,'tick',return_value={'phase':'ready'}) as tick,patch.object(pipeline,'run',return_value={}):
            runner.scheduled()
        self.assertEqual(task['blocker'],'annual_no_progress');tick.assert_called_once()

    def test_current_queue_runs_before_any_full_housekeeping(self):
        from stock_research import current_batch as job
        with patch.object(core,'config',return_value={'automatic_enabled':True}),patch.object(job,'managed',return_value=True),patch.object(job,'active',return_value=True),patch.object(job,'continuous',return_value=True),patch.object(job,'load',side_effect=[{'cursor':1},{'cursor':2},{'cursor':2}]),patch.object(core,'run_lock',return_value=nullcontext()),patch.object(job,'tick',side_effect=[{'phase':'ready'},{'phase':'waiting_idle'}]) as tick,patch.object(pipeline,'run') as run:
            result=runner.scheduled()
        run.assert_not_called();self.assertEqual(tick.call_count,2)
        self.assertEqual(result['current_batch']['phase'],'waiting_idle')

    def test_waiting_current_queue_does_not_start_long_housekeeping(self):
        from stock_research import current_batch as job
        with patch.object(core,'config',return_value={'automatic_enabled':True}),patch.object(job,'managed',return_value=True),patch.object(job,'active',return_value=True),patch.object(job,'continuous',return_value=False),patch.object(job,'status',return_value={'phase':'waiting_idle'}),patch.object(pipeline,'run') as run,patch.object(job,'tick') as tick:
            result=runner.scheduled()
        run.assert_not_called();tick.assert_not_called()
        self.assertEqual(result['current_batch']['phase'],'waiting_idle')

    def test_current_no_progress_still_stops_repeated_paid_work(self):
        from stock_research import current_batch as job
        task={}
        with patch.object(core,'config',return_value={'automatic_enabled':True}),patch.object(job,'managed',return_value=True),patch.object(job,'active',return_value=True),patch.object(job,'continuous',return_value=True),patch.object(job,'load',return_value={'cursor':1}),patch.object(core,'run_lock',return_value=nullcontext()),patch.object(job,'tick',return_value={'phase':'ready'}),patch.object(job,'edit',return_value=nullcontext(task)),patch.object(job,'status',return_value={'phase':'blocked'}),patch.object(pipeline,'run') as run:
            runner.scheduled()
        run.assert_not_called();self.assertEqual(task['blocker'],'annual_no_progress')
