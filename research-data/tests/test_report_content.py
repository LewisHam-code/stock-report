import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from stock_research import sources
from stock_research.report_content import filter_report_pages


class ReportContentTests(unittest.TestCase):
    def test_generic_glossary_removed_but_entities_units_and_business_are_kept(self):
        pages=[{'page':1,'text':'第一节 释义\n\n中国证监会\t指\t中国证券监督管理委员会\n\n控股股东\t指\t某投资集团\n\n矿业甲\t指\t新收购的甲公司\n\n元、万元\t指\t人民币元、万元'},
               {'page':2,'text':'第二节 公司简介和主要财务指标\n\n电话\t123456\n\n公司的法定代表人\t某人\n\n单位：万元\n营业收入100\n归母净利润20'},
               {'page':3,'text':'第三节 管理层讨论与分析\n\n主要业务为采矿，经营现金流-100万元。\n\n四、重要会计政策\n\n收入确认存在重大判断。'}]
        saved=copy.deepcopy(pages);filtered,omissions=filter_report_pages(pages)
        self.assertEqual(pages,saved)
        text='\n'.join(p['text'] for p in filtered)
        self.assertNotIn('中国证券监督管理委员会',text)
        self.assertNotIn('123456',text)
        for value in ('某投资集团','新收购的甲公司','人民币元、万元','法定代表人','营业收入100','经营现金流-100','收入确认存在重大判断'):
            self.assertIn(value,text)
        self.assertTrue(omissions)

    def test_contents_page_requires_real_leaders_and_protects_financial_rows(self):
        pages=[{'page':1,'text':'目录\n第一节 释义..........2\n第二节 公司简介..........3\n第三节 财务..........4'},
               {'page':2,'text':'目录\n第一节..........2\n第二节..........3\n第三节..........4\n营业收入100.00万元'}]
        filtered,omissions=filter_report_pages(pages)
        self.assertEqual(filtered[0]['text'],'')
        self.assertIn('100.00',filtered[1]['text'])
        self.assertEqual(len(omissions),1)

    def test_complete_page_selection_is_not_mislabeled_as_unabridged_text(self):
        pages=[{'page':1,'text':'目录\n第一节 释义..........2\n第二节 公司简介..........3\n第三节 财务..........4'},
               {'page':2,'text':'第一节 释义\n\n报告期\t指\t2025年全年\n\n控股股东\t指\t某投资集团'},
               {'page':3,'text':'第二节 公司简介和主要财务指标\n\n电话\t123456\n\n营业收入100万元'}]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'text.json').write_text(json.dumps({'pages':pages}))
            with patch.object(sources,'ROOT',root):
                raw,original=sources.excerpt({'text_file':'text.json'},max_chars=1000)
                filtered,coverage=sources.excerpt({'text_file':'text.json'},max_chars=1000,omit_administrative=True)
        self.assertTrue(original['full_text'])
        self.assertFalse(coverage['full_text'])
        self.assertTrue(coverage['all_source_pages_selected'])
        self.assertEqual(coverage['read_pages'],[2,3])
        self.assertIn('[PDF第3页]',filtered)
        self.assertLess(len(filtered),len(raw))

    def test_unknown_layout_and_business_containing_admin_label_are_retained(self):
        text='第二节 公司简介\n\n电子信箱\t未披露；主要业务发生变更，存在重大风险。\n\n重要会计政策：采用加速折旧。'
        filtered,_=filter_report_pages([{'page':1,'text':text}])
        self.assertEqual(filtered[0]['text'],text)


if __name__=='__main__':unittest.main()
