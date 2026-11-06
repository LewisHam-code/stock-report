import json
import pathlib
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from stock_research import core, deployment
from stock_research.core import ServiceError


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cfg = core.config()
        cfg.update(automatic_enabled=False, interval_minutes=60)
        (self.root / 'automation.json').write_text(json.dumps(cfg))
        py = self.root / '.venv/bin/python3'
        py.parent.mkdir(parents=True)
        py.write_text('test placeholder')
        for module,name,value in ((core,'ROOT',self.root),(deployment,'ROOT',self.root),(deployment,'PROJECT',self.root),
                                  (deployment,'AGENTS',self.root/'agents'),(deployment,'LOGS',self.root/'logs')):
            self.stack.enter_context(patch.object(module,name,value))
        self.stack.enter_context(patch.object(deployment,'job_loaded',return_value=False))
        self.system = self.stack.enter_context(patch.object(deployment,'launch',return_value=subprocess.CompletedProcess([],0)))

    def data(self, **changes):
        return dict(enabled=True,interval_minutes=30,daily_api_calls=32,analysis_per_run=4,download_per_run=20,**changes)

    def test_enable_registers_fixed_project_commands_and_interval(self):
        before = core.config().get('model_profiles')
        result = deployment.save_schedule(self.data())
        spec = plistlib.loads((self.root/'agents'/f'{deployment.WORKER}.plist').read_bytes())
        self.assertEqual(spec['StartInterval'],1800)
        self.assertEqual(spec['ProgramArguments'][-1],'scheduled')
        self.assertEqual(spec['Umask'],0o077)
        self.assertTrue(result['settings']['enabled'])
        self.assertEqual(core.config().get('model_profiles'),before)
        self.assertTrue(any(c.args[0][0]=='kickstart' for c in self.system.call_args_list))

    def test_invalid_budget_never_changes_system(self):
        data=self.data();data['daily_api_calls']=-1
        with self.assertRaises(ServiceError):deployment.save_schedule(data)
        self.system.assert_not_called()

    def test_invalid_timing_never_changes_system_and_explicit_choice_is_saved(self):
        data=self.data();data['prefer_off_peak']='false'
        with self.assertRaises(ServiceError):deployment.save_schedule(data)
        self.system.assert_not_called()
        data['prefer_off_peak']=False
        self.assertFalse(deployment.save_schedule(data)['settings']['prefer_off_peak'])

    def test_disabled_job_does_not_run_on_login(self):
        spec=deployment.job_spec(deployment.WORKER,60)
        self.assertFalse(spec['RunAtLoad'])
        self.assertTrue(deployment.job_spec(deployment.SERVICE)['KeepAlive'])

    def test_execution_settings_save_during_research_without_changing_jobs(self):
        cfg=core.config();cfg.update(automatic_enabled=True,interval_minutes=30)
        core.atomic_json(self.root/'automation.json',cfg)
        with core.run_lock(),patch.object(deployment,'job_loaded',return_value=True):
            result=deployment.save_schedule({**self.data(),'prefer_off_peak':True,'quarter_workers':10,'financial_reading_limit_cny':.5})
        self.assertTrue(result['settings']['prefer_off_peak']);self.assertEqual(result['settings']['quarter_workers'],10)
        self.assertEqual(result['settings']['financial_reading_limit_cny'],.5)
        self.system.assert_not_called()

    def test_invalid_concurrency_or_cost_is_rejected_before_any_mutation(self):
        original=core.config()
        for change in ({'quarter_workers':21},{'quarter_workers':True},{'financial_reading_limit_cny':0},{'financial_reading_limit_cny':float('nan')}):
            with self.assertRaises(ServiceError):deployment.save_schedule({**self.data(),**change})
        self.assertEqual(core.config(),original);self.system.assert_not_called()

    def test_registration_failure_restores_original_config(self):
        before=core.config()
        def fail_worker(args,required=True):
            if args[0]=='bootstrap' and str(args[-1]).endswith(deployment.WORKER+'.plist'):
                raise ServiceError('system_schedule_failed','test')
            return subprocess.CompletedProcess([],0)
        self.system.side_effect=fail_worker
        with self.assertRaises(ServiceError):deployment.save_schedule(self.data())
        self.assertEqual(core.config(),before)

    def test_running_research_is_not_terminated_when_saving_schedule(self):
        with core.run_lock():
            with self.assertRaises(ServiceError) as caught:deployment.save_schedule(self.data())
        self.assertEqual(caught.exception.code,'research_running')
        self.assertFalse(any(c.args[0][0]=='bootout' for c in self.system.call_args_list))

    def test_saving_enabled_budget_does_not_start_another_paid_round(self):
        cfg=core.config();cfg['automatic_enabled']=True
        core.atomic_json(self.root/'automation.json',cfg)
        with patch.object(deployment,'job_loaded',return_value=True):
            deployment.save_schedule(self.data())
        self.assertFalse(any(c.args[0][0]=='kickstart' for c in self.system.call_args_list))

    def test_immediate_start_failure_does_not_claim_saved_schedule_failed(self):
        def fail_kick(args,required=True):
            if args[0]=='kickstart':raise ServiceError('system_schedule_failed','placeholder')
            return subprocess.CompletedProcess([],0)
        self.system.side_effect=fail_kick
        state=deployment.save_schedule(self.data())
        self.assertTrue(core.config()['automatic_enabled'])
        self.assertIn('warning',state)


if __name__=='__main__':unittest.main()
