from datetime import date

from app.roi import build_roi_report, month_end, render_roi_html, render_roi_markdown
from tests.test_pcc import activity, boston

TODAY = date(2026, 10, 1)


def test_month_end():
    assert month_end(date(2026, 9, 1)) == date(2026, 9, 30) and month_end(date(2026, 2, 1)) == date(2026, 2, 28)


def test_roi_statement():
    r = build_roi_report(activity(), {"Greater Boston": boston()}, date(2026, 9, 1), TODAY, fee=1500, margin=0.6)
    b = next(m for m in r["markets"] if m["market"] == "Greater Boston")
    assert b["booked"] == 17
    assert b["sold"] == 4 and b["revenue"] == 1460          # jobs 1, 9, 15 and 17
    assert b["ahead"] == 3                                   # two scheduled jobs and one scheduled lead
    assert b["lost"] == 3                                    # canceled, Done at $0, lost lead
    assert b["return"] == 1460 / 1500 and b["gross_return"] == 1460 * 0.6 / 1500
    assert "Greater Boston" not in r["clears_fee"] and not r["partial"]
    assert "Return on fee" in render_roi_markdown(r) and "Greater Boston" in render_roi_html(r)
