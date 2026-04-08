import datetime
import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star import StarMetadata, star_registry
from astrbot.core.star.star_tools import StarTools

from .data import ScheduleDataManager
from .generator import SchedulerGenerator
from .schedule import LifeScheduler
from .utils import time_desc


class LifeSchedulerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.schedule_data_file = self.data_dir / "schedule_data.json"
        plugins_dir = Path(__file__).resolve().parent.parent
        self.astrbot_data_dir = plugins_dir.parent
        self.config_dir = self.astrbot_data_dir / "config"
        self.schema_path = Path(__file__).with_name("_conf_schema.json")

    async def initialize(self):
        self._refresh_provider_schema_options()
        self.data_mgr = ScheduleDataManager(self.schedule_data_file)
        self.generator = SchedulerGenerator(self.context, self.config, self.data_mgr)
        self.scheduler = LifeScheduler(
            context=self.context,
            config=self.config,
            task=self.generator.generate_schedule,
        )
        self.scheduler.start()

    async def terminate(self):
        """插件卸载时清理"""
        self.scheduler.stop()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self._refresh_provider_schema_options()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata: StarMetadata):
        self._refresh_provider_schema_options()

    def _refresh_provider_schema_options(self) -> None:
        provider_ids: list[str] = [""]
        providers = getattr(getattr(self.context, "provider_manager", None), "providers", None)
        if providers:
            for provider in providers:
                provider_id = str(getattr(provider, "id", "") or "").strip()
                if provider_id and provider_id not in provider_ids:
                    provider_ids.append(provider_id)

        if len(provider_ids) == 1:
            cmd_config_path = self.astrbot_data_dir / "cmd_config.json"
            try:
                cmd_config = json.loads(cmd_config_path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                logger.warning("[LifeScheduler] load cmd_config for schema refresh failed: %s", exc)
                cmd_config = {}
            for provider_cfg in cmd_config.get("provider", []) or []:
                provider_id = str(provider_cfg.get("id") or "").strip()
                if provider_id and provider_id not in provider_ids:
                    provider_ids.append(provider_id)

        try:
            schema = json.loads(self.schema_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning("[LifeScheduler] load schema for provider refresh failed: %s", exc)
            return

        field = schema.get("schedule_provider_id")
        if not isinstance(field, dict):
            return

        schema_changed = field.get("options") != provider_ids
        if schema_changed:
            field["options"] = provider_ids
            field["default"] = field.get("default", "")
            try:
                self.schema_path.write_text(
                    json.dumps(schema, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("[LifeScheduler] write schema provider options failed: %s", exc)

        try:
            metadata = star_registry.get(self.__class__.__name__)
            live_schema = metadata.config.schema if metadata and metadata.config else None
            live_field = self._find_schema_field(live_schema, "schedule_provider_id")
            if isinstance(live_field, dict) and live_field.get("options") != provider_ids:
                live_field["options"] = list(provider_ids)
        except Exception as exc:
            logger.warning("[LifeScheduler] update live schema provider options failed: %s", exc)

        logger.info(
            "[LifeScheduler] refreshed provider options: count=%s source=%s",
            len(provider_ids) - 1,
            "runtime" if providers else "cmd_config",
        )

    def _find_schema_field(self, schema: object, field_name: str) -> dict | None:
        if isinstance(schema, dict):
            direct = schema.get(field_name)
            if isinstance(direct, dict):
                return direct
            for key in ("items", "properties", "fields"):
                nested = schema.get(key)
                found = self._find_schema_field(nested, field_name)
                if found:
                    return found
            return None

        if isinstance(schema, list):
            for item in schema:
                found = self._find_schema_field(item, field_name)
                if found:
                    return found

        return None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """System Prompt 注入"""
        today = datetime.datetime.now()
        umo = event.unified_msg_origin
        data = self.data_mgr.get(today)
        if not data:
            try:
                data = await self.generator.generate_schedule(today, umo)
            except RuntimeError:
                return
        if data.status == "failed":
            return

        inject_text = f"""
<character_state>
时间: {time_desc()}
穿着: {data.outfit}
日程: {data.schedule}
</character_state>
[上述状态仅供需要时参考，无需主动提及]"""

        req.system_prompt += inject_text
        logger.debug(f"[LLM] 添加的内在状态注入：{inject_text}")

    @filter.command("查看日程", alias={"life show"})
    async def life_show(self, event: AstrMessageEvent):
        """查看今日的日程"""
        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        umo = event.unified_msg_origin

        data = self.data_mgr.get(today)
        if not data:
            try:
                yield event.plain_result("今日还没日程，正在生成...")
                data = await self.generator.generate_schedule(today, umo)
            except RuntimeError:
                yield event.plain_result("日程正在生成中，请稍后再查看")
                return
        yield event.plain_result(
            f"📅 {today_str}\n👗 今日穿搭：{data.outfit}\n📝 日程安排：\n{data.schedule}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重写日程", alias={"life renew"})
    async def life_renew(self, event: AstrMessageEvent, extra: str | None = None):
        """重写今日的日程，可附加补充要求。用法：重写日程 [补充要求]"""
        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        umo = event.unified_msg_origin
        if extra:
            yield event.plain_result(f"正在根据补充要求重写今日日程：{extra}")
        else:
            yield event.plain_result("正在重写今日日程...")
        try:
            data = await self.generator.generate_schedule(today, umo, extra=extra)
        except RuntimeError:
            yield event.plain_result("已有日程生成任务在进行中，请稍后再试")
            return
        yield event.plain_result(
            f"📅 {today_str}\n👗 今日穿搭：{data.outfit}\n📝 日程安排：{data.schedule}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程时间", alias={"life time"})
    async def life_time(self, event: AstrMessageEvent, param: str | None = None):
        """日程时间 [HH:MM] ，设置每日日程生成时间"""
        if not param:
            yield event.plain_result("请提供时间，格式为 HH:MM，例如 /life time 07:30")
            return

        # 支持 1~2 位小时、1~2 位分钟，中间用冒号分隔
        if not re.match(r"^\d{1,2}:\d{1,2}$", param):
            yield event.plain_result("时间格式错误，请使用 HH:MM 格式")
            return

        # 再补一层范围校验，防止 99:99 这类非法时间
        try:
            hour, minute = map(int, param.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            yield event.plain_result(
                "时间格式错误，请使用 HH:MM 格式，且小时 0-23、分钟 0-59"
            )
            return

        try:
            self.scheduler.update_schedule_time(param)
            yield event.plain_result(f"已将每日日程生成时间更新为 {param}。")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")
