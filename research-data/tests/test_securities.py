import io
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stock_research import core,securities as s
from stock_research.core import Store,ServiceError,TZ


class SecurityMasterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name,value in [('VAULT',self.root/'vault'),('ROOT',self.root/'runtime')]:
            p=patch.object(core,name,value);p.start();self.addCleanup(p.stop)
        self.store=Store(self.root/'test.sqlite');self.addCleanup(self.store.conn.close)

    def row(self,code='601988',name='中国银行',status='listed'):
        return s.security(code,name,'SH','https://www.sse.com.cn/',full_name='中国银行股份有限公司',status=status,
                          delisted_at='2026-10-01' if status=='delisted' else '')

    def seed(self,rows,**extra):
        core.atomic_json(s.master_path(),{'schema':1,'securities':rows,**extra})

    def test_name_full_name_alias_and_code_share_one_verified_identity(self):
        row=self.row();row['aliases']=['旧银行'];self.seed([row])
        with patch.object(s,'refresh',side_effect=AssertionError('unexpected network')):
            for term in ('中国银行','中国银行股份有限公司','旧银行','601988'):
                self.assertEqual(s.resolve(self.store,term)['code'],'601988')
        self.assertEqual(s.search('中国银')[0]['company'],'中国银行')

    def test_unknown_code_is_not_accepted_by_prefix_and_fallback_is_cached(self):
        self.seed([])
        with patch.object(s,'refresh'),patch.object(s,'cninfo_rows',return_value=[]) as fetch:
            for _ in range(2):
                with self.assertRaises(ServiceError):s.resolve(self.store,'600999')
            self.assertEqual(fetch.call_count,1)

    def test_explicit_delisted_record_blocks_new_follow_and_price_requests(self):
        self.seed([self.row(status='delisted')])
        with self.assertRaises(ServiceError) as caught:s.resolve(self.store,'601988')
        self.assertEqual(caught.exception.code,'security_delisted')
        self.assertFalse(s.price_enabled('601988'))

    def run_refresh(self,listed,retired=()):
        def sh(kind='listed'):return list(retired) if kind=='delisted' else listed
        with patch.object(s,'MINIMUM',{'SH':1,'SZ':0,'BJ':0}),patch.object(s,'sse_rows',side_effect=sh), \
             patch.object(s,'szse_rows',return_value=[]),patch.object(s,'bse_rows',return_value=[]),patch.object(s,'cninfo_rows',return_value=[]):
            return s.refresh(force=True)

    def test_rename_st_and_removal_are_logged_without_rewriting_old_aliases(self):
        self.seed([self.row()]);self.run_refresh([self.row(name='*ST中行')])
        row=s.lookup('601988');self.assertEqual(row['risk_flag'],'*ST');self.assertIn('中国银行',row['aliases'])
        self.assertTrue(s.master_path().with_name('证券变更记录.jsonl').is_file())
        self.run_refresh([self.row(name='新中行')]);self.assertEqual(s.lookup('601988')['risk_flag'],'')
        self.assertEqual(s.resolve(self.store,'中国银行')['company'],'新中行')

    def test_missing_entry_is_review_not_delisting_and_partial_feed_is_rejected(self):
        self.seed([self.row(),self.row('600000','浦发银行')])
        # A 50% loss fails the completeness gate; neither entry disappears.
        result=self.run_refresh([self.row()]);self.assertIn('SH',[e.get('market') for e in result['errors']])
        self.assertEqual(s.lookup('600000')['status'],'listed')
        rows=[self.row(str(600000+i),f'公司{i}') for i in range(10)]
        self.seed(rows);self.run_refresh(rows[:9])
        self.assertEqual(s.lookup('600009')['status'],'pending_confirmation')
        self.assertTrue(s.price_enabled('600009'))

    def test_daily_refresh_checks_again_after_next_nine_am_boundary(self):
        self.seed([self.row()],refresh_cycle='2026-10-18',last_attempt_at='2026-10-19T05:00:00+08:00',errors=[])
        with patch.object(s,'now',return_value=datetime(2026,10,19,8,tzinfo=TZ)),patch.object(s,'sse_rows') as fetch:
            self.assertEqual(s.refresh()['state'],'cached');fetch.assert_not_called()
        with patch.object(s,'now',return_value=datetime(2026,10,19,9,tzinfo=TZ)), \
             patch.object(s,'sse_rows',side_effect=ServiceError('offline','offline')),patch.object(s,'szse_rows',side_effect=ServiceError('offline','offline')), \
             patch.object(s,'bse_rows',side_effect=ServiceError('offline','offline')),patch.object(s,'cninfo_rows',return_value=[]):
            self.assertNotEqual(s.refresh()['state'],'cached')

    def test_official_inline_xlsx_text_and_leading_zero_are_preserved(self):
        data=io.BytesIO()
        xml='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>A股代码</t></is></c></row><row><c r="A2" t="inlineStr"><is><t>000001</t></is></c></row></sheetData></worksheet>'
        with zipfile.ZipFile(data,'w') as archive:archive.writestr('xl/worksheets/sheet1.xml',xml)
        self.assertEqual(s.xlsx_rows(data.getvalue())[0]['a股代码'],'000001')

    def test_quote_name_difference_requests_confirmation_only_once(self):
        self.seed([self.row()]);s.note_quote_identity(self.store,'601988',{'company':'ST中行'})
        self.assertTrue(self.store.setting('security_catalog_refresh_requested'))
        self.assertEqual(s.lookup('601988')['company'],'中国银行')

    def test_future_termination_does_not_disable_current_prices(self):
        row=s.security('601988','中国银行','SH',s.SSE,status='delisted',delisted_at='2099-01-01')
        self.assertEqual(row['status'],'delisting_announced');self.seed([row]);self.assertTrue(s.price_enabled('601988'))
        missing=s.security('600000','浦发银行','SH',s.SSE,status='delisted')
        self.assertEqual(missing['status'],'pending_confirmation')

    def test_corrupt_master_keeps_last_valid_memory_index(self):
        self.seed([self.row()]);self.assertIsNotNone(s.lookup('601988'))
        s.master_path().write_text('{broken json')
        self.assertEqual(s.lookup('601988')['company'],'中国银行')
        self.assertEqual(s.load()[0]['read_error'],'catalog_unreadable')

    def test_unchanged_state_does_not_rewrite_company_notes_each_day(self):
        folder=core.VAULT/'30_公司/SH/601988';folder.mkdir(parents=True)
        company=folder/'公司.md';company.write_text('---\ncompany: "中国银行"\n---\n我的批注\n')
        row=self.row();self.seed([row]);s.sync_events([], [row])
        note=folder/'证券状态-自动生成.md';before=(note.stat().st_mtime_ns,company.stat().st_mtime_ns)
        row['verified_at']='2026-10-20T09:00:00+08:00';s.sync_events([], [row])
        self.assertEqual(before,(note.stat().st_mtime_ns,company.stat().st_mtime_ns))
        self.assertIn('我的批注',company.read_text())
