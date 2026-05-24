"""
官方仪表盘统计插件入口。

API 端点：
  GET /stats/uploads    — 跨所有网盘聚合：今日 / 本周 / 本月上传字节数
  GET /stats/quotas     — 每个网盘的 day/week/month 上传量 + 按 range 聚合的序列

数据源：
  - quota_usage (app.models.quota.QuotaUsage)  —— 每盘每日上传统计
  - context.list_drives()                       —— 网盘列表
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Query

# 让同目录模块可被导入（框架使用 spec_from_file_location，无包身份）
_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from app.plugin.base import PluginContext, PluginInterface, PluginMeta
from app.models.quota import QuotaUsage


def _bucket(usage_date: date, today: date) -> List[str]:
    """单条记录归属的窗集合（一条记录可能同时算入 day/week/month）。"""
    out = []
    if usage_date == today:
        out.append("day")
    if usage_date >= today - timedelta(days=6):
        out.append("week")
    if usage_date >= today - timedelta(days=29):
        out.append("month")
    return out


def _empty_series(today: date) -> List[Dict[str, object]]:
    """生成 30 天全零序列（覆盖 today-29 .. today）。"""
    start = today - timedelta(days=29)
    return [{"date": (start + timedelta(days=i)).isoformat(), "bytes": 0}
            for i in range(30)]


# ── Range 聚合 ──
# day:   过去 30 天，逐日
# month: 过去 12 个月，逐月（每月第一日为 bucket key）
# year:  过去 5 年，逐年（每年 1/1 为 bucket key）

_RANGES = {"day", "month", "year"}


def _range_window(today: date, rng: str) -> date:
    if rng == "month":
        # 起点：12 个月前的当月 1 号
        m = today.month - 11
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        return date(y, m, 1)
    if rng == "year":
        return date(today.year - 4, 1, 1)
    # default day
    return today - timedelta(days=29)


def _bucket_key(d: date, rng: str) -> date:
    """把一个 usage_date 归到对应 bucket 的起始日。"""
    if rng == "month":
        return date(d.year, d.month, 1)
    if rng == "year":
        return date(d.year, 1, 1)
    return d


def _all_buckets(today: date, rng: str) -> List[date]:
    """按时间序生成所有 bucket key。"""
    if rng == "month":
        out: List[date] = []
        m, y = today.month - 11, today.year
        while m <= 0:
            m += 12
            y -= 1
        for _ in range(12):
            out.append(date(y, m, 1))
            m += 1
            if m > 12:
                m = 1
                y += 1
        return out
    if rng == "year":
        return [date(today.year - i, 1, 1) for i in range(4, -1, -1)]
    start = today - timedelta(days=29)
    return [start + timedelta(days=i) for i in range(30)]


# ── 插件主体 ──

class DashboardStatsPlugin(PluginInterface):

    def __init__(self):
        self._context: Optional[PluginContext] = None
        manifest_path = Path(__file__).parent / "manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as f:
            self._meta = PluginMeta(**json.load(f))

    def get_meta(self) -> PluginMeta:
        return self._meta

    async def on_load(self, context: PluginContext) -> None:
        self._context = context

        router = APIRouter()

        @router.get("/uploads")
        async def uploads_stats(
            range: str = Query("day", pattern="^(day|month|year)$",
                               description="聚合粒度：day=过去 30 天逐日 / month=过去 12 月 / year=过去 5 年"),
        ):
            """跨所有启用网盘的 day/week/month 总量 + 按 range 聚合的折线序列。

            注：`quota_usage` 历史记录不随网盘删除而清理，必须按当前 active 网盘 ID 过滤，
            否则会把早已下线的盘的旧数据也算进总量。
            """
            today = date.today()
            month_start = today - timedelta(days=29)

            visible_ids = {d["id"] for d in context.list_drives() if d.get("is_active")}
            if not visible_ids:
                return {
                    "today": today.isoformat(),
                    "range": range,
                    "totals": {"day": 0, "week": 0, "month": 0},
                    "series": [{"date": k.isoformat(), "bytes": 0}
                               for k in _all_buckets(today, range)],
                }

            window_start = _range_window(today, range)
            # totals 永远基于过去 30 天，与 range 无关
            totals_start = min(window_start, month_start)

            db = context.get_db()
            try:
                rows = db.query(QuotaUsage).filter(
                    QuotaUsage.usage_date >= totals_start,
                ).all()
            finally:
                db.close()

            buckets: Dict[date, int] = {k: 0 for k in _all_buckets(today, range)}
            totals = {"day": 0, "week": 0, "month": 0}
            for r in rows:
                if r.drive_config_id not in visible_ids:
                    continue
                size = int(r.bytes_used or 0)
                if r.usage_date >= window_start:
                    key = _bucket_key(r.usage_date, range)
                    if key in buckets:
                        buckets[key] += size
                for w in _bucket(r.usage_date, today):
                    totals[w] += size

            return {
                "today": today.isoformat(),
                "range": range,
                "totals": totals,
                "series": [{"date": k.isoformat(), "bytes": buckets[k]}
                           for k in sorted(buckets)],
            }

        @router.get("/quotas")
        async def quotas_stats(
            range: str = Query("day", pattern="^(day|month|year)$",
                               description="聚合粒度：day / month / year"),
        ):
            """每个网盘的 day/week/month 上传量 + 按 range 聚合的序列。"""
            today = date.today()
            window_start = _range_window(today, range)
            month_start = today - timedelta(days=29)
            totals_start = min(window_start, month_start)

            all_drives = [d for d in context.list_drives() if d.get("is_active")]
            if not all_drives:
                return {"today": today.isoformat(), "range": range, "drives": []}

            drive_ids = {d["id"] for d in all_drives}
            bucket_keys = _all_buckets(today, range)

            db = context.get_db()
            try:
                rows = db.query(QuotaUsage).filter(
                    QuotaUsage.usage_date >= totals_start,
                ).all()
            finally:
                db.close()

            by_drive: Dict[int, Dict[str, int]] = {
                d["id"]: {"day": 0, "week": 0, "month": 0} for d in all_drives
            }
            series_by_drive: Dict[int, Dict[date, int]] = {
                d["id"]: {k: 0 for k in bucket_keys} for d in all_drives
            }
            for r in rows:
                if r.drive_config_id not in drive_ids:
                    continue
                size = int(r.bytes_used or 0)
                if r.usage_date >= window_start:
                    key = _bucket_key(r.usage_date, range)
                    if key in series_by_drive[r.drive_config_id]:
                        series_by_drive[r.drive_config_id][key] += size
                for w in _bucket(r.usage_date, today):
                    by_drive[r.drive_config_id][w] += size

            result = []
            for d in all_drives:
                result.append({
                    "id":          d["id"],
                    "name":        d["name"],
                    "drive_type":  d["drive_type"],
                    "uploaded":    by_drive[d["id"]],
                    "series":      [{"date": k.isoformat(),
                                     "bytes": series_by_drive[d["id"]][k]}
                                    for k in sorted(series_by_drive[d["id"]])],
                })

            return {"today": today.isoformat(), "range": range, "drives": result}

        context.register_router(router, prefix="/stats", tags=["仪表盘统计"])
        context.logger.info("[DashboardStatsPlugin] Loaded")

    async def on_unload(self) -> None:
        if self._context:
            self._context.logger.info("[DashboardStatsPlugin] Unloaded")
        self._context = None
