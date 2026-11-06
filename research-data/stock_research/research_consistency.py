"""Conservative, auditable display corrections against the shared fact set.

Original database text is untouched. Ambiguous semantic comparisons are flagged
for review rather than turned into a fabricated new business explanation.
"""
from copy import deepcopy
import re

from .financial_facts import trusted_metrics
from .core import finite

VERSION='narrative-fact-consistency-v2'


def apply(report):
    result=deepcopy(report)
    facts=list(trusted_metrics(report.get('financial_analysis') or {}))
    by={(m['metric'],m['scope']):m for m in facts}
    changes=[];findings=[]
    names={'扣非净利润':'adjusted_profit','扣非归母净利润':'adjusted_profit','归母净利润':'net_profit'}
    targets=[(field,result,field,'') for field in ('conclusion','evidence','valuation','risks')]
    for n,section in enumerate((result.get('financial_analysis') or {}).get('sections') or []):
        if isinstance(section,dict):targets.append((f'financial_analysis.sections.{n}.analysis',section,'analysis',str(section.get('title',''))))
    current_year=str(report.get('period',''))[:4]
    chronology=re.compile(r'20\d{2}年|去年|上年同期|上年|本年|今年|本期')
    scope_tokens=re.compile(r'年初至报告期末|本报告期累计|本期累计|累计(?:前三季度)?|前三季度|上半年|全年|单季度|第三季度|本季度')
    amount_pattern=re.compile(r'(?P<label>扣非归母净利润|扣非净利润|归母净利润)(?P<join>(?:为|达到|实现|约为|约|达)?\s*)(?P<number>-?\d[\d,]*(?:\.\d+)?)(?P<space>\s*)(?P<unit>亿元|万元|元)')
    for field,container,key,title in targets:
        original=container.get(key,'')
        if not isinstance(original,str):continue
        text=original
        # Carry an explicitly declared scope through semicolon-separated
        # clauses in the same sentence (e.g. cumulative sales; net profit).
        # Reset at full stops/newlines and reject prior-period amount clauses.
        title_years=re.findall(r'(20\d{2})年',title)
        title_prior=bool(title_years and any(y!=current_year for y in title_years))
        scope=None;prior=title_prior;parts=[]
        for part in re.split(r'([。；\n])',text):
            if part in ('。','\n'):
                parts.append(part);scope=None;prior=title_prior;continue
            if part=='；':parts.append(part);continue
            initial_scope,initial_prior=scope,prior
            def state_at(position):
                active_scope,active_prior=initial_scope,initial_prior
                for token in scope_tokens.finditer(part[:position]):
                    active_scope='single_quarter' if token.group() in ('单季度','第三季度','本季度') else 'cumulative'
                for token in chronology.finditer(part[:position]):
                    value=token.group()
                    active_prior=value in ('去年','上年同期','上年') or (value.endswith('年') and value[:4].isdigit() and value[:4]!=current_year)
                return active_scope,active_prior
            # Quoted source text is evidence, not an editable model assertion.
            if not title_prior and not any(q in original for q in ('“','”','「','」','"')):
                def repair_clause(match):
                    active_scope,active_prior=state_at(match.start())
                    if not active_scope or active_prior:return match.group()
                    metric=names[match['label']];known=by.get((metric,active_scope))
                    if not known or finite(known.get('standard_value')) is None:return match.group()
                    amount=float(match['number'].replace(',',''));unit={'亿元':1e8,'万元':1e4,'元':1}[match['unit']]
                    digits=len(match['number'].partition('.')[2]);expected=known['standard_value']/unit
                    if abs(amount-expected)<=.5*10**(-digits)+1e-10:return match.group()
                    replacement=match['label']+match['join']+f'{expected:.{digits}f}'+match['space']+match['unit']
                    changes.append({'field':field,'kind':'amount','before':match.group(),'after':replacement,'metric':metric,'scope':active_scope,'source_page':known.get('source_page'),'fact_id':known.get('fact_id')})
                    return replacement
                part=amount_pattern.sub(repair_clause,part)
                for label,metric in names.items():
                    missing=re.compile(re.escape(label)+r'(?:数据)?(?:尚未通过财报原文核对|尚未核实|未核实|未提取|尚未提取)')
                    def status_fix(match):
                        active_scope,active_prior=state_at(match.start())
                        fact=by.get((metric,active_scope or 'cumulative'))
                        if active_prior or not fact:return match.group()
                        replacement=label+'已由原文核对（第'+str(fact.get('source_page'))+'页）'
                        changes.append({'field':field,'kind':'verification_status','before':match.group(),'after':replacement,'fact_id':fact.get('fact_id')})
                        return replacement
                    part=missing.sub(status_fix,part)
                    stale_status=re.compile('('+re.escape(label)+r'\s*(-?\d[\d,.]*)\s*(亿元|万元|元))（财报原文，待核实）')
                    def stale_fix(match):
                        active_scope,active_prior=state_at(match.start())
                        fact=by.get((metric,active_scope or 'cumulative'))
                        if active_prior or not fact or finite(fact.get('standard_value')) is None:return match.group()
                        digits=len(match[2].partition('.')[2]);unit={'亿元':1e8,'万元':1e4,'元':1}[match[3]]
                        if abs(float(match[2].replace(',',''))-fact['standard_value']/unit)>.5*10**(-digits)+1e-10:return match.group()
                        replacement=match[1]+'（已核对原文，第'+str(fact.get('source_page'))+'页）'
                        changes.append({'field':field,'kind':'verification_status','before':match.group(),'after':replacement,'fact_id':fact.get('fact_id')})
                        return replacement
                    part=stale_status.sub(stale_fix,part)
            scope,prior=state_at(len(part))
            parts.append(part)
        text=''.join(parts)
        comparison=re.compile(r'[^。；\n]*应收[^。；\n]*(?:增速高于收入|快于营收|快于收入|高于营收增速|高于收入增速)[^。；\n]*[。；]?')
        context='\n'.join(str(report.get(k,'')) for k in ('conclusion','evidence','risks'))
        if any(w in context for w in ('较期初','较年初','从年初')):
            for match in list(comparison.finditer(text)):
                findings.append({'field':field,'kind':'comparison_basis','text':match.group(),'reason':'应收/存货期初变化与收入同比口径不同，原增长快慢结论已撤回'})
            if field=='conclusion':
                relation=re.compile(r'(?:存货(?:与|及|和))?应收(?:账款)?(?:的)?(?:增速|增长速度|增长)?(?:高于收入(?:增速)?|快于(?:营收|收入)(?:增速)?)')
                match=relation.search(text)
                if match:
                    replacement='应收与存货变动的比较口径仍待核实'
                    changes.append({'field':field,'kind':'comparison_basis','before':match.group(),'after':replacement})
                    text=text[:match.start()]+replacement+text[match.end():]
        for sentence in re.findall(r'[^。；\n]+[。；]?',text):
            if ('应收' in sentence and any(w in sentence for w in ('快于营收','快于收入','高于营收增速','高于收入增速'))
                    and any(w in sentence for w in ('期初','年初','同比'))):
                findings.append({'field':field,'kind':'comparison_basis','text':sentence,'reason':'应收时点与营收期间/同比可能不可比，不能直接证明应收增长更快'})
        if text!=original:
            result.setdefault('original_narrative_before_consistency',{})[field]=original
            container[key]=text
    # A source quotation does not establish the inference made from it.
    # Frontend collapses flagged original narrative, and shows the reason.
    result['narrative_consistency']={'version':VERSION,'changes':changes,'findings':findings,
                                     'state':'review' if findings else 'corrected' if changes else 'checked',
                                     'note':'只修复明确本期且期间一致的金额/状态；历史、引文与未知口径不自动改写。比较口径存疑时提示复核，不新增模型调用。'}
    return result
