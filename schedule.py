from __future__ import annotations

import zoneinfo
from collections.abc import Awaitable, Callable

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

TaskCallable = Callable[[], Awaitable[object | None]]


class LifeScheduler:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        task: TaskCallable,
    ):
        self.config = config
        self.task = task
        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )
        self.scheduler = AsyncIOScheduler(
            timezone=self.timezone,
            executors={"default": AsyncIOExecutor()},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 120,
            },
        )
        self.job = None

    def start(self):
        try:
            schedule_time = str(self.config["schedule_time"] or "07:00")
            hour, minute, second = self._parse_schedule_time(schedule_time)
            self.job = self.scheduler.add_job(
                self.task,
                "cron",
                hour=hour,
                minute=minute,
                second=second,
                id="daily_schedule_gen",
            )
            self.scheduler.start()
            logger.info("生活调度器已启动，时间：%s", schedule_time)
        except Exception as exc:
            logger.error("调度器初始化失败：%s", exc)

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()

    def update_schedule_time(self, new_time: str):
        if new_time == self.config["schedule_time"]:
            return

        try:
            hour, minute, second = self._parse_schedule_time(new_time)
            self.config["schedule_time"] = new_time
            self.config.save_config()
            if self.job:
                self.job.reschedule("cron", hour=hour, minute=minute, second=second)
                logger.info(
                    "生活调度器已重新排程至 %02d:%02d:%02d",
                    hour,
                    minute,
                    second,
                )
        except Exception as exc:
            logger.error("更新调度器失败：%s", exc)

    @staticmethod
    def _parse_schedule_time(value: str) -> tuple[int, int, int]:
        parts = [part.strip() for part in str(value or "").split(":")]
        if len(parts) == 2:
            hour, minute = map(int, parts)
            second = 0
        elif len(parts) == 3:
            hour, minute, second = map(int, parts)
        else:
            raise ValueError("时间格式必须是 HH:MM 或 HH:MM:SS")
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise ValueError("时间超出有效范围")
        return hour, minute, second
