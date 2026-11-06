"""Five research grades; evidence status is separate from investment direction."""
LABELS = {"buy": "买入", "overweight": "增持", "neutral": "中性", "underweight": "减持", "sell": "卖出", "unrated": "未评级"}
POLICY = "本系统研究分档：买入≥80且有买点，增持≥65，中性≥45，减持≥25，卖出<25；证据充分后才分档。未达到买点的高分归增持，风险约束可下调评级。不是券商原版收益预测或下单指令；阈值尚未经样本外验证。"


def classify(scoring, missing, red_flags, technical):
    blockers = list(dict.fromkeys(list(missing)+scoring.get("provisional_reasons",[])))
    if scoring.get("status") != "calculated" and not blockers:
        blockers.append("评分所需证据尚未补齐")
    if blockers or scoring.get("status") != "calculated":
        return {"rating": "unrated", "evidence_status": "insufficient", "rating_policy": POLICY, "rating_blockers": blockers}
    score = scoring["score"]
    if score >= 80 and technical.get("buy_signal"):
        rating = "buy"
    elif score >= 65:
        rating = "overweight"
    elif score >= 45:
        rating = "neutral"
    elif score >= 25:
        rating = "underweight"
    else:
        rating = "sell"
    if red_flags:
        ceiling = "sell" if any("风险警示" in s or "非正" in s for s in red_flags) else "underweight"
        grades = ["sell", "underweight", "neutral", "overweight", "buy"]
        rating = grades[min(grades.index(rating), grades.index(ceiling))]
    return {"rating": rating, "evidence_status": "sufficient", "rating_policy": POLICY, "rating_blockers": []}
