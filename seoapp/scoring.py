"""Priority scoring for recommendations.

score (0-100) = impact x confidence x ease, boosted for high-priority keywords.
- impact 1-10: expected effect on rankings/traffic/leads if done
- effort 1-10: 1 = minutes, 3 = an hour, 6 = a day, 10 = a project
- confidence 0-1: how sure we are it will help
"""

LEVELS = ["critical", "high", "medium", "low"]


def compute(impact: int, effort: int, confidence: float, keyword_priority: int = 0,
            category: str = "") -> tuple[float, str]:
    impact = max(1, min(10, int(impact)))
    effort = max(1, min(10, int(effort)))
    confidence = max(0.1, min(1.0, float(confidence)))
    ease = (12 - effort) / 11  # 1.0 for effort 1, ~0.18 for effort 10
    boost = 1.0 + 0.06 * max(0, keyword_priority - 2)  # priority 5 keyword -> x1.18
    score = round(min(100.0, impact * 10 * confidence * (0.45 + 0.55 * ease) * boost), 1)
    if (impact >= 9 and category == "technical") or score >= 70:
        level = "critical"
    elif score >= 48:
        level = "high"
    elif score >= 28:
        level = "medium"
    else:
        level = "low"
    return score, level
