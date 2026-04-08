import ast
import asyncio
import datetime
import json
import random
import re
from dataclasses import asdict, dataclass
from typing import Any

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .data import (
    ScheduleData,
    ScheduleDataManager,
    ScheduleSegment,
    build_default_segments,
    build_segment_slots,
    normalize_clock_text,
    resolve_cycle_anchor,
)

_STYLE_PREFIX_RE = re.compile(
    r"^\s*(?:风格|【风格】|\[风格\])\s*[:：]\s*(?P<style>.+?)(?:\n|$)"
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_KEY_VALUE_LINE_RE = re.compile(
    r"^\s*(outfit_style|summary_outfit|summary_schedule|outfit|schedule|穿搭风格|今日穿搭|穿搭|今日安排|日程)\s*[:：]\s*(.*)$"
)
_TOOL_PLACEHOLDER_RE = re.compile(
    r"(i am ready to help|i'?m ready to help|available tools|我已准备好帮助完成任务)",
    re.IGNORECASE,
)

_SEGMENT_KEYS = (
    "wake_up",
    "morning_outing",
    "daytime_work",
    "after_work",
    "home_evening",
    "late_night",
)


@dataclass(slots=True)
class ScheduleContext:
    date_str: str
    weekday: str
    holiday: str
    persona_desc: str
    history_schedules: str
    recent_chats: str
    daily_theme: str
    mood_color: str
    outfit_style: str
    schedule_type: str
    anchor_time: str
    window_start: str
    window_end: str
    segment_slots_text: str


class SchedulerGenerator:
    _STYLE_ENFORCE_RETRIES = 2
    _EMPTY_COMPLETION_RETRIES = 1

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        data_mgr: ScheduleDataManager,
    ):
        self.context = context
        self.config = config
        self.data_mgr = data_mgr
        self._gen_lock = asyncio.Lock()
        self._generating = False

    async def generate_schedule(
        self,
        date: datetime.datetime | None = None,
        umo: str | None = None,
        extra: str | None = None,
    ) -> ScheduleData:
        async with self._gen_lock:
            if self._generating:
                raise RuntimeError("schedule_generating")
            self._generating = True

        data: ScheduleData | None = None
        moment = date or datetime.datetime.now()
        anchor_time = normalize_clock_text(str(self.config.get("schedule_time") or "07:00"))
        anchor_dt = resolve_cycle_anchor(moment, anchor_time)
        anchor_key = anchor_dt.date().isoformat()

        try:
            logger.info(
                "正在生成 %s 的固定日程窗口，anchor=%s",
                anchor_key,
                anchor_dt.strftime("%Y-%m-%d %H:%M"),
            )
            ctx = await self._collect_context(moment, anchor_dt, anchor_time, umo)
            prompt = self._build_prompt(ctx, extra)
            sid_base = f"life_scheduler_gen_{anchor_key}"
            content = await self._call_llm(prompt, sid=f"{sid_base}_0")

            payload = self._extract_json_obj(content)
            ok, reason = self._validate_payload(payload, ctx)
            last_content = content
            for attempt in range(1, self._STYLE_ENFORCE_RETRIES + 1):
                if ok:
                    break
                repair_prompt = self._build_style_repair_prompt(ctx, last_content, reason)
                last_content = await self._call_llm(repair_prompt, sid=f"{sid_base}_{attempt}")
                payload = self._extract_json_obj(last_content)
                ok, reason = self._validate_payload(payload, ctx)

            if not ok or not payload:
                raise ValueError(f"模型未遵循日程结构约束：{reason}")

            data = self._to_schedule_data(payload, anchor_dt, ctx)
            logger.info(
                "固定日程生成成功: %s",
                json.dumps(asdict(data), ensure_ascii=False, indent=2)[:1200],
            )
            return data
        except Exception as exc:
            logger.error("日程生成失败: %s", exc)
            data = self._build_local_fallback_schedule(anchor_dt, ctx if "ctx" in locals() else None, extra=extra)
            return data
        finally:
            async with self._gen_lock:
                self._generating = False
            if data:
                self.data_mgr.set(data)

    async def _collect_context(
        self,
        moment: datetime.datetime,
        anchor_dt: datetime.datetime,
        anchor_time: str,
        umo: str | None,
    ) -> ScheduleContext:
        slots = build_segment_slots(anchor_dt)
        slot_lines = [
            f"- {slot['key']} | {slot['label']} | {slot['start_time']}-{slot['end_time']}"
            for slot in slots
        ]
        return ScheduleContext(
            date_str=moment.strftime("%Y年%m月%d日"),
            weekday=self._weekday(moment),
            holiday=self._get_holiday_info(moment.date()),
            persona_desc=await self._get_persona(),
            history_schedules=self._get_history(moment.date()),
            recent_chats=await self._get_recent_chats(umo),
            **self._pick_diversity(moment.date()),
            anchor_time=anchor_time,
            window_start=anchor_dt.strftime("%Y-%m-%d %H:%M"),
            window_end=(anchor_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            segment_slots_text="\n".join(slot_lines),
        )

    def _weekday(self, date: datetime.datetime) -> str:
        return [
            "星期一",
            "星期二",
            "星期三",
            "星期四",
            "星期五",
            "星期六",
            "星期日",
        ][date.weekday()]

    def _get_holiday_info(self, date: datetime.date) -> str:
        try:
            import holidays

            cn_holidays = holidays.CN()
            holiday_name = cn_holidays.get(date)
            if holiday_name:
                return f"今天是{holiday_name}"
        except Exception:
            return ""
        return ""

    def _pick_diversity(self, today: datetime.date) -> dict[str, str]:
        pool = self.config["pool"]
        return {
            "daily_theme": random.choice(pool["daily_themes"]),
            "mood_color": random.choice(pool["mood_colors"]),
            "outfit_style": self._pick_outfit_style(pool["outfit_styles"], today),
            "schedule_type": random.choice(pool["schedule_types"]),
        }

    def _pick_outfit_style(self, styles: list[str], today: datetime.date) -> str:
        styles = list(styles or [])
        if not styles:
            return ""

        lookback_days = int(self.config.get("reference_history_days", 0) or 0)
        if lookback_days <= 0 or len(styles) <= 1:
            return random.choice(styles)

        used: set[str] = set()
        for i in range(1, lookback_days + 1):
            hist_date = today - datetime.timedelta(days=i)
            data = self.data_mgr.get(hist_date)
            if not data or data.status != "ok":
                continue
            style = (getattr(data, "outfit_style", "") or "").strip()
            if not style:
                style = self._extract_style_from_outfit(data.outfit)
            if style:
                used.add(style)

        candidates = [style for style in styles if style not in used]
        return random.choice(candidates or styles)

    def _extract_style_from_outfit(self, outfit: str) -> str:
        if not outfit:
            return ""
        match = _STYLE_PREFIX_RE.match(outfit.strip())
        if not match:
            return ""
        return (match.group("style") or "").strip()

    def _get_history(self, today: datetime.date) -> str:
        items: list[str] = []
        days = int(self.config.get("reference_history_days", 0) or 0)
        if days <= 0:
            return "（无历史记录）"

        for i in range(1, days + 1):
            hist_date = today - datetime.timedelta(days=i)
            data = self.data_mgr.get(hist_date)
            if not data or data.status != "ok":
                continue
            style = (
                (getattr(data, "outfit_style", "") or "").strip()
                or self._extract_style_from_outfit(data.outfit)
            )
            summary_outfit = (data.summary_outfit or data.outfit or "")[:60]
            summary_schedule = (data.summary_schedule or data.schedule or "")[:80]
            items.append(
                f"[{hist_date.strftime('%Y-%m-%d')}] 风格：{style} 全天穿搭：{summary_outfit} 全天安排：{summary_schedule}"
            )
        return "\n".join(items) if items else "（无历史记录）"

    async def _get_recent_chats(
        self,
        umo: str | None = None,
        count: int | None = None,
    ) -> str:
        count = count or self.config["reference_recent_count"]
        if not umo or not count:
            return "无近期对话"

        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return "无最近对话记录"
            conv = await self.context.conversation_manager.get_conversation(umo, cid)
            if not conv or not conv.history:
                return "无最近对话记录"
            history = json.loads(conv.history)
            recent = history[-count:] if count > 0 else []

            formatted: list[str] = []
            for msg in recent:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if role == "user":
                    formatted.append(f"用户: {content}")
                elif role == "assistant":
                    formatted.append(f"我: {content}")
            return "\n".join(formatted) if formatted else "无最近对话记录"
        except Exception as exc:
            logger.error("Failed to get recent chats for %s: %s", umo, exc)
            return "获取对话记录失败"

    async def _get_persona(self) -> str:
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            return (
                persona.get("prompt")
                if isinstance(persona, dict)
                else getattr(persona, "prompt", "")
            )
        except Exception:
            return "你是一个热爱生活、情感细腻的 AI 伙伴。"

    def _build_prompt(self, ctx: ScheduleContext, extra: str | None = None) -> str:
        ctx_dict = asdict(ctx)
        template = str(self.config["prompt_template"] or "")
        tmpl_vars = set(re.findall(r"\{(\w+)\}", template))
        for key in tmpl_vars - ctx_dict.keys():
            ctx_dict[key] = ""

        prompt = template.format(**ctx_dict)
        prompt += (
            "\n\n## 24小时固定窗口\n"
            f"- 这份日程从 {ctx.window_start} 开始生效，到 {ctx.window_end} 结束，中间 24 小时内保持同一套人物状态。\n"
            f"- 刷新锚点时间固定为 {ctx.anchor_time}，在下一个刷新锚点到来前，不要改写当天设定。\n"
            "- 你要给出全天摘要，以及每个时间段的细致穿搭、活动、地点、情绪和自拍线索。\n"
            "- 不同时间段穿搭必须有层次变化：起床居家、出门工作、白天状态、下班返程、回家后、夜间休息，不能全部写成同一套衣服。\n"
            "- 自拍线索必须贴合该时间段，而不是写成泛泛而谈的文生图描述。\n"
            "\n## 固定时间段\n"
            f"{ctx.segment_slots_text}\n"
            "\n## 输出要求\n"
            "- 只输出 JSON 对象本体，不要 Markdown，不要代码块，不要解释。\n"
            f'- 字段 "outfit_style" 必须严格等于 "{ctx.outfit_style}"。\n'
            '- 字段 "summary_outfit" 写全天主线穿搭概括。\n'
            '- 字段 "summary_schedule" 写全天安排概括。\n'
            '- 字段 "segments" 必须包含 wake_up、morning_outing、daytime_work、after_work、home_evening、late_night 六段。\n'
            '- 每个 segment 至少包含 outfit、activity、location、mood、selfie_scene、selfie_prompt_hint、caption_hint。\n'
            "\n## 输出示例\n"
            "{\n"
            f'  "outfit_style": "{ctx.outfit_style}",\n'
            '  "summary_outfit": "风格：...\\n全天主线穿搭概括",\n'
            '  "summary_schedule": "一句话概括今天的 24 小时状态",\n'
            '  "segments": {\n'
            '    "wake_up": {\n'
            '      "outfit": "起床后在家的穿搭",\n'
            '      "activity": "起床后的具体活动",\n'
            '      "location": "场景地点",\n'
            '      "mood": "情绪状态",\n'
            '      "selfie_scene": "此时段自拍长什么样",\n'
            '      "selfie_prompt_hint": "改图时要强调什么",\n'
            '      "caption_hint": "此时段自拍说说应有的口吻"\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
        if extra:
            prompt += f"\n\n【用户补充要求】\n{extra}"
        return prompt

    async def _call_llm(self, prompt: str, *, sid: str = "life_scheduler_gen") -> str:
        provider = self._get_provider(sid)
        if not provider:
            raise RuntimeError("No provider")
        provider_name = self._get_provider_debug_name(provider)
        logger.info("[LifeScheduler] generating schedule with provider=%s", provider_name)
        try:
            for attempt in range(self._EMPTY_COMPLETION_RETRIES + 1):
                resp = await provider.text_chat(prompt, session_id=sid)
                text = self._extract_completion_text(resp)
                if text and not _TOOL_PLACEHOLDER_RE.search(text.strip()):
                    return text
                if attempt < self._EMPTY_COMPLETION_RETRIES:
                    logger.warning("LLM completion 为空或命中占位回复，准备重试一次")
            raise RuntimeError("API 返回的 completion 为空或是占位回复")
        finally:
            await self._cleanup_session(sid)

    def _get_provider(self, origin: str | None = None):
        provider_id = str(self.config.get("schedule_provider_id") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                logger.debug("[LifeScheduler] use configured provider: %s", provider_id)
                return provider
            except Exception as exc:
                logger.warning(
                    "[LifeScheduler] configured provider unavailable: %s error=%s",
                    provider_id,
                    exc,
                )
        try:
            return self.context.get_using_provider(origin)
        except TypeError:
            return self.context.get_using_provider()

    @staticmethod
    def _get_provider_debug_name(provider: object) -> str:
        for attr in ("id", "provider_id", "model", "name"):
            value = getattr(provider, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return provider.__class__.__name__

    @staticmethod
    def _extract_completion_text(resp: object) -> str:
        if resp is None:
            return ""
        if isinstance(resp, dict):
            for key in ("completion_text", "completion", "text", "content"):
                value = resp.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            choices = resp.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0]
                if isinstance(choice, dict):
                    for key in ("text", "content"):
                        value = choice.get(key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
                    message = choice.get("message")
                    if isinstance(message, dict):
                        value = message.get("content")
                        if isinstance(value, str) and value.strip():
                            return value.strip()
        for key in ("completion_text", "completion", "text", "content"):
            value = getattr(resp, key, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def _cleanup_session(self, sid: str):
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(sid)
            if cid:
                await self.context.conversation_manager.delete_conversation(sid, cid)
        except Exception:
            pass

    def _extract_json_obj(self, text: str) -> dict[str, Any] | None:
        candidates = self._collect_payload_candidates(text)
        for candidate in candidates:
            payload = self._try_parse_payload(candidate)
            if payload:
                return payload
        return self._extract_key_value_payload(text)

    def _collect_payload_candidates(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        candidates: list[str] = [text]
        candidates.extend(match.group(1).strip() for match in _JSON_FENCE_RE.finditer(text))
        candidates.extend(self._extract_braced_json_candidates(text))
        seen: set[str] = set()
        result: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _extract_braced_json_candidates(self, text: str) -> list[str]:
        result: list[str] = []
        stack = 0
        start = -1
        in_string = False
        escape = False
        for idx, ch in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                if stack == 0:
                    start = idx
                stack += 1
            elif ch == "}":
                if stack <= 0:
                    continue
                stack -= 1
                if stack == 0 and start != -1:
                    result.append(text[start : idx + 1].strip())
                    start = -1
        return result

    def _try_parse_payload(self, candidate: str) -> dict[str, Any] | None:
        candidate = self._normalize_json_like_text(candidate)
        if not candidate:
            return None
        for loader in (json.loads, ast.literal_eval):
            try:
                data = loader(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                return self._coerce_payload(data)
        return None

    def _normalize_json_like_text(self, text: str) -> str:
        text = (text or "").strip().lstrip("\ufeff")
        replacements = {
            "“": '"',
            "”": '"',
            "‘": "'",
            "’": "'",
            "：": ":",
        }
        for src, dst in replacements.items():
            text = text.replace(src, dst)
        text = re.sub(r",\s*([}\]])", r"\1", text)
        return text

    def _extract_key_value_payload(self, text: str) -> dict[str, Any] | None:
        text = (text or "").strip()
        if not text:
            return None

        data: dict[str, Any] = {}
        current_key: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _KEY_VALUE_LINE_RE.match(line)
            if match:
                current_key = match.group(1)
                data[current_key] = match.group(2).strip()
                continue
            if current_key:
                previous = str(data.get(current_key) or "")
                data[current_key] = (previous + "\n" + line).strip() if previous else line

        if not data:
            return None
        return self._coerce_payload(data)

    def _coerce_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        aliases = {
            "outfit_style": ("outfit_style", "穿搭风格"),
            "summary_outfit": ("summary_outfit", "今日穿搭", "穿搭", "outfit"),
            "summary_schedule": ("summary_schedule", "今日安排", "日程", "schedule"),
        }

        def pick(alias_keys: tuple[str, ...]) -> str:
            for key in alias_keys:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        payload: dict[str, Any] = {
            "outfit_style": pick(aliases["outfit_style"]),
            "summary_outfit": pick(aliases["summary_outfit"]),
            "summary_schedule": pick(aliases["summary_schedule"]),
        }

        segments = data.get("segments")
        if isinstance(segments, (dict, list)):
            payload["segments"] = segments
        else:
            segment_map: dict[str, Any] = {}
            for key in _SEGMENT_KEYS:
                value = data.get(key)
                if isinstance(value, dict):
                    segment_map[key] = value
            if segment_map:
                payload["segments"] = segment_map

        return payload

    def _validate_payload(
        self,
        payload: dict[str, Any] | None,
        ctx: ScheduleContext,
    ) -> tuple[bool, str]:
        if not payload:
            return False, "未能解析出 JSON 对象"

        summary_outfit = str(payload.get("summary_outfit", "")).strip()
        summary_schedule = str(payload.get("summary_schedule", "")).strip()
        if not summary_outfit:
            return False, "summary_outfit 不能为空"
        if not summary_schedule:
            return False, "summary_schedule 不能为空"

        required = (ctx.outfit_style or "").strip()
        if required:
            model_style = str(payload.get("outfit_style", "")).strip()
            if model_style != required:
                return False, f'outfit_style 必须严格等于 "{required}"'

        return True, ""

    def _build_style_repair_prompt(
        self,
        ctx: ScheduleContext,
        bad_text: str,
        reason: str,
    ) -> str:
        return (
            "你之前的输出没有通过校验，需要按要求重写。\n"
            f"失败原因：{reason}\n"
            f"必须使用的穿搭风格：{ctx.outfit_style}\n"
            "你必须只输出一个 JSON 对象，并补齐 summary_outfit、summary_schedule、segments 六段结构。\n"
            "不要解释，不要 Markdown，不要代码块。\n\n"
            "你上一次的输出如下（可能不合规，仅供参考）：\n"
            f"{bad_text}"
        )

    def _normalize_segments(
        self,
        payload: dict[str, Any],
        *,
        anchor_dt: datetime.datetime,
        outfit_style: str,
        summary_outfit: str,
        summary_schedule: str,
    ) -> list[ScheduleSegment]:
        default_segments = build_default_segments(
            anchor_dt=anchor_dt,
            outfit_style=outfit_style,
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
        )
        slot_map = {item["key"]: item for item in build_segment_slots(anchor_dt)}
        raw_segments = payload.get("segments")
        raw_map: dict[str, Any] = {}

        if isinstance(raw_segments, list):
            for item in raw_segments:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if key:
                    raw_map[key] = item
        elif isinstance(raw_segments, dict):
            raw_map = raw_segments

        normalized: list[ScheduleSegment] = []
        for default in default_segments:
            slot = slot_map.get(default.key, {})
            raw = raw_map.get(default.key)
            if not isinstance(raw, dict):
                raw = {}
            normalized.append(
                ScheduleSegment(
                    key=default.key,
                    label=slot.get("label", default.label),
                    start_time=slot.get("start_time", default.start_time),
                    end_time=slot.get("end_time", default.end_time),
                    outfit=str(raw.get("outfit") or default.outfit).strip(),
                    activity=str(raw.get("activity") or raw.get("schedule") or default.activity).strip(),
                    location=str(raw.get("location") or default.location).strip(),
                    mood=str(raw.get("mood") or default.mood).strip(),
                    selfie_scene=str(raw.get("selfie_scene") or default.selfie_scene).strip(),
                    selfie_prompt_hint=str(raw.get("selfie_prompt_hint") or default.selfie_prompt_hint).strip(),
                    caption_hint=str(raw.get("caption_hint") or default.caption_hint).strip(),
                )
            )
        return normalized

    def _to_schedule_data(
        self,
        payload: dict[str, Any],
        anchor_dt: datetime.datetime,
        ctx: ScheduleContext,
    ) -> ScheduleData:
        outfit_style = str(payload.get("outfit_style") or ctx.outfit_style or "").strip()
        summary_outfit = str(payload.get("summary_outfit") or "").strip() or f"风格：{outfit_style}\n以 {outfit_style} 为主线安排全天穿搭。"
        summary_schedule = str(payload.get("summary_schedule") or "").strip() or "今天按自己的节奏处理工作、生活和休息。"
        segments = self._normalize_segments(
            payload,
            anchor_dt=anchor_dt,
            outfit_style=outfit_style,
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
        )
        return ScheduleData(
            date=anchor_dt.date().isoformat(),
            anchor_time=ctx.anchor_time,
            window_start=anchor_dt.isoformat(timespec="seconds"),
            window_end=(anchor_dt + datetime.timedelta(days=1)).isoformat(timespec="seconds"),
            outfit_style=outfit_style,
            outfit=summary_outfit,
            schedule=summary_schedule,
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
            segments=segments,
            status="ok",
        ).with_defaults()

    def _build_local_fallback_schedule(
        self,
        anchor_dt: datetime.datetime,
        ctx: ScheduleContext | None,
        *,
        extra: str | None = None,
    ) -> ScheduleData:
        outfit_style = (ctx.outfit_style if ctx else "") or "自然日常风"
        summary_outfit = (
            f"风格：{outfit_style}\n"
            f"今天以 {outfit_style} 为主线，早晚层次不同，出门阶段更完整利落，回家后换成更舒服的状态。"
        )
        summary_schedule = (
            "这一天从早上整理状态开始，白天处理工作或学习，傍晚收尾回家，晚上把节奏放慢下来。"
        )
        if extra:
            summary_schedule += f" 额外要求会体现在当天安排里：{extra}"
        segments = build_default_segments(
            anchor_dt=anchor_dt,
            outfit_style=outfit_style,
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
        )
        return ScheduleData(
            date=anchor_dt.date().isoformat(),
            anchor_time=ctx.anchor_time if ctx else normalize_clock_text(str(self.config.get("schedule_time") or "07:00")),
            window_start=anchor_dt.isoformat(timespec="seconds"),
            window_end=(anchor_dt + datetime.timedelta(days=1)).isoformat(timespec="seconds"),
            outfit_style=outfit_style,
            outfit=summary_outfit,
            schedule=summary_schedule,
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
            segments=segments,
            status="ok",
        )
