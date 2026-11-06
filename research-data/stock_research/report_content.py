"""Auditable removal of clerical annual-report content, never business policies.

This is an explicit model-input option. Original extracted pages stay intact.
Unknown layouts are retained rather than guessed or summarized by an AI.
"""
import re
from collections import Counter

VERSION='administrative-v1'
GENERIC_TERMS={'中国证监会','证监会','上交所','深交所','北交所','公开发行股票','A股',
               '报告期','本报告期','报告期末','《公司法》','公司法','《证券法》','证券法'}
ADMIN_FIELDS=('公司的中文名称','公司的中文简称','公司的外文名称缩写','公司的外文名称',
 '公司办公地址的邮政编码','公司注册地址','公司办公地址','公司网址',
 '公司披露年度报告的媒体名称及网址','公司披露年度报告的证券交易所网址','公司年度报告备置地点',
 '联系地址','电子信箱','电话','传真')
IMPORTANT=re.compile(r'营业收入|净利润|现金流|资产负债|股本|注册资本|审计意见|会计师事务所|'
                     r'实际控制人|控股股东|关联交易|重大风险|诉讼|处罚|会计政策|会计估计|主要业务|经营模式')


def filter_report_pages(pages):
    result=[];omissions=[];glossary=False;company=False
    for page in pages:
        text=page['text'];flat=re.sub(r'\s','',text)
        # Require a genuine contents heading and several leader/page entries.
        if (re.match(r'^目录',flat) and len(re.findall(r'\.{5,}\s*\d+',text))>=3
                and not re.search(r'\d[,.]\d{2}|[%％]',text)):
            result.append({**page,'text':''})
            omissions.append({'page':page['page'],'reason':'目录及备查文件目录','blocks':1,'removed_characters':len(text)})
            continue
        kept=[];removed=Counter();removed_chars=Counter()
        for paragraph in re.split(r'\n\s*\n',text):
            compact=re.sub(r'\s','',paragraph)
            heading=re.match(r'^第[一二三四五六七八九十百]+节([^\n]+)',paragraph.strip())
            if heading:
                title=re.sub(r'\s','',heading[1])
                glossary=title.startswith('释义')
                company=title.startswith(('公司简介','公司信息'))
            reason=None
            if glossary:
                term=re.match(r'^(.{1,80}?)\s+指\s+',paragraph.strip())
                if term and re.sub(r'\s','',term[1]) in GENERIC_TERMS:reason='通用缩写及重复报告期释义'
                elif compact in ('在本报告书中，除非文义另有所指，下列词语具有如下含义：','常用词语释义'):
                    reason='释义表引导语'
            if company and not IMPORTANT.search(paragraph):
                for label in ADMIN_FIELDS:
                    if re.match(r'^'+re.escape(label)+r'(?:[：:\t ]|$)',paragraph.strip()):
                        reason='重复身份或行政联系方式';break
            if reason:
                removed[reason]+=1;removed_chars[reason]+=len(paragraph)
            else:kept.append(paragraph)
        for reason,n in removed.items():
            omissions.append({'page':page['page'],'reason':reason,'blocks':n,'removed_characters':removed_chars[reason]})
        result.append({**page,'text':'\n\n'.join(kept).strip()})
    return result,omissions
