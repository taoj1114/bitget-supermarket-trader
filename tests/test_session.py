"""美股时段判定测试(周末/盘中/盘后), 用于非交易日停机逻辑。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from supermarket.market import us_session

ET = ZoneInfo("America/New_York")


def test_weekend():
    # 2026-09-12 是周六
    sat = datetime(2026, 9, 12, 12, 0, tzinfo=ET)
    assert us_session(sat) == "weekend"
    # 2026-09-13 周日
    sun = datetime(2026, 9, 13, 12, 0, tzinfo=ET)
    assert us_session(sun) == "weekend"


def test_regular_hours():
    # 2026-09-10 周四 10:00 ET → regular
    t = datetime(2026, 9, 10, 10, 0, tzinfo=ET)
    assert us_session(t) == "regular"


def test_pre_post_closed():
    # 07:00 ET 盘前
    pre = datetime(2026, 9, 10, 7, 0, tzinfo=ET)
    assert us_session(pre) == "pre_market"
    # 17:00 ET 盘后
    post = datetime(2026, 9, 10, 17, 0, tzinfo=ET)
    assert us_session(post) == "post_market"
    # 21:00 ET 收盘后
    closed = datetime(2026, 9, 10, 21, 0, tzinfo=ET)
    assert us_session(closed) == "closed"