"""Rejected experimental length prompts, kept to reproduce the recorded A/B test.

Not imported by the normal analysis pipeline: lower visible length did not
reliably reduce reasoning and some qualifiers were lost in the test.
"""

VERSION = 'concise-evidence-v1'


def instructions(stage, mode='initial'):
    deep = mode == 'deep'
    common = '\n输出精简规则：同一事实、数字或证据只解释一次，其他字段只写它的新含义；不得为凑齐主题重复免责声明。以下篇幅为表达目标，不得省略重大风险、冲突、关键证据或必要的口径说明；不足时可超出。不要为了接近输出上限扩写。'
    if stage == 'extraction':
        return common + ('\n财务提取：metrics保留全部13类指标及必要的原始单位、期间、页码和逐字引文，不能为省字删改引文或遗漏未知指标。'
                         'sections同一主题合并，只写有信息增量的事实、异常及待核实点；常规主题每项以' + ('180' if deep else '100') + '个汉字为目标，重大异常可详写。'
                         '缺披露主题可集中列入uncertainties，不能误称不存在或已核实。management_claims仅保留影响判断的独立经营主张，不复制财务指标表；risks与uncertainties去重。')
    return common + ('\n综合分析：不要重新誊写财务提取的全部分项和指标表；优先说明影响结论的变化、反证与因果尚未确认之处。'
                     'conclusion以80字以内为目标；evidence以' + ('900' if deep else '550') + '字以内、risks以' + ('500' if deep else '320') + '字以内为目标。'
                     'valuation只陈述已有估值证据与局限，缺少同行或历史比较时简要说明，不在多个字段反复展开同一缺口。'
                     'catalysts、falsification各优先列3个独立关键条件；scenarios仍保留悲观、基准、乐观3种，每种只列不同的假设与影响。'
                     '证据页码、支持与反对因素、关键风险、可证伪条件以及事实/推断区别必须保留；没有可靠证据时明确写未知。')
