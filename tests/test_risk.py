# tests/test_risk.py
from app.agent.risk import score


def _diff_with(body: str, target: str = "app.py") -> str:
    return f"--- a/{target}\n+++ b/{target}\n@@ -1,1 +1,1 @@\n{body}"


def test_score_low_for_small_clean_diff():
    diff = _diff_with("+x = 2\n-x = 1\n")
    assert score(diff, has_test_coverage=True) < 0.3


def test_score_higher_without_test_coverage():
    diff = _diff_with("+x = 2\n-x = 1\n")
    with_tests = score(diff, has_test_coverage=True)
    without_tests = score(diff, has_test_coverage=False)
    assert without_tests > with_tests


def test_score_higher_for_sensitive_tokens():
    diff = _diff_with("+password = get_secret()\n-password = None\n")
    plain = _diff_with("+x = 2\n-x = 1\n")
    assert score(diff, has_test_coverage=True) > score(plain, has_test_coverage=True)


def test_score_capped_at_one():
    body = "".join(f"+password token secret session sql auth line{i}\n" for i in range(500))
    diff = _diff_with(body)
    assert score(diff, has_test_coverage=False) == 1.0
