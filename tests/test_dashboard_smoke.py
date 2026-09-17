"""Headless dashboard smoke test (issue #14 recurrence prevention).

Runs dashboard/app.py via streamlit's AppTest harness: the app must execute
without raising, and the 5-tab bar must be present. Skipped when streamlit
isn't importable (e.g. minimal CI images); the CI workflow installs it.
"""
import pytest

streamlit = pytest.importorskip("streamlit")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

EXPECTED_TABS = ["RFQs", "Pricing & quoting", "Performance",
                 "Engine status", "NFL correlation"]


def test_dashboard_renders_without_exceptions():
    at = AppTest.from_file("dashboard/app.py")
    at.run()
    assert not at.exception, f"dashboard raised: {at.exception!r}"


def test_dashboard_has_five_tabs():
    at = AppTest.from_file("dashboard/app.py")
    at.run()
    assert not at.exception, f"dashboard raised: {at.exception!r}"
    assert len(at.tabs) == 5, f"expected 5 tabs, got {len(at.tabs)}"
    labels = [t.label for t in at.tabs]
    assert labels == EXPECTED_TABS, f"tab labels: {labels}"
