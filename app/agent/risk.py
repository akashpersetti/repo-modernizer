from app.services.diffs import parse_unified_diff

SENSITIVE_TOKENS = ["password", "secret", "token", "session", "sql", "auth"]
_LINES_NORMALIZER = 200


def score(diff_text: str, has_test_coverage: bool) -> float:
    parsed = parse_unified_diff(diff_text)
    lines_component = min(parsed.lines_changed / _LINES_NORMALIZER, 1.0) * 0.4
    lowered = diff_text.lower()
    sensitive_component = 0.4 if any(tok in lowered for tok in SENSITIVE_TOKENS) else 0.0
    test_component = 0.0 if has_test_coverage else 0.2
    return round(min(lines_component + sensitive_component + test_component, 1.0), 3)
