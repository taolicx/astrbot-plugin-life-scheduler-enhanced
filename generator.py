from __future__ import annotations

import asyncio
import datetime
import json
import random
import re
import sys
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .data import (
    ScheduleData,
    ScheduleDataManager,
    build_detailed_segments,
    build_segment_slots,
    normalize_clock_text,
    resolve_cycle_anchor,
)

_TOOL_PLACEHOLDER_RE = re.compile(
    r"(i am ready to help|i'?m ready to help|available tools|我已准备好帮助完成任务)",
    re.IGNORECASE,
)
_MARKDOWN_FENCE_RE = re.compile(
    r"```(?:text|markdown|md|json)?\s*(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•+]|[0-9]+[.)、])\s*")
_LABEL_LINE_RE = re.compile(
    r"^\s*(今日主线|日程主线|主线安排|穿搭重点|白天重点|晚间状态|夜间状态|自拍氛围|整体气质|补充细节|daily hook|outfit focus|daytime focus|evening focus|selfie tone|vibe)\s*[:：]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")

_SEGMENT_KEYS = (
    "wake_up",
    "morning_outing",
    "daytime_work",
    "after_work",
    "home_evening",
    "late_night",
)


def compat_dataclass(*args, **kwargs):
    """兼容旧版 Python，对 slots 参数做降级处理。"""
    if sys.version_info < (3, 10):
        kwargs = dict(kwargs)
        kwargs.pop("slots", None)
    return dataclass(*args, **kwargs)


@compat_dataclass(slots=True)
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


@compat_dataclass(slots=True)
class DayGuidance:
    daily_hook: str = ""
    outfit_focus: str = ""
    daytime_focus: str = ""
    evening_focus: str = ""
    selfie_tone: str = ""
    vibe: str = ""
    raw_text: str = ""

    def has_content(self) -> bool:
        return any(
            (
                self.daily_hook,
                self.outfit_focus,
                self.daytime_focus,
                self.evening_focus,
                self.selfie_tone,
                self.vibe,
            )
        )


class SchedulerGenerator:
    """基于轻量文本线索生成固定窗口生活日程。

    这一版不再要求模型直接返回严格 JSON，而是让模型只给出少量文本线索，
    然后由代码本地组装全天摘要和 6 段详细时段，降低不同模型格式波动导致的整条失败。
    """

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

        moment = date or datetime.datetime.now()
        anchor_time = normalize_clock_text(str(self.config.get("schedule_time") or "07:00"))
        anchor_dt = resolve_cycle_anchor(moment, anchor_time)
        date_key = anchor_dt.date().isoformat()
        logger.info("正在生成 %s 的日程...", date_key)

        ctx: ScheduleContext | None = None
        try:
            ctx = await self._build_context(anchor_dt, umo=umo)
            guidance = await self._collect_guidance(anchor_dt, ctx, extra=extra)
            if guidance.has_content():
                data = self._build_schedule_from_guidance(
                    anchor_dt,
                    ctx,
                    guidance,
                    extra=extra,
                )
            else:
                logger.warning("[LifeScheduler] 模型未提供可用线索，改用本地模板。")
                data = self._build_local_fallback_schedule(
                    anchor_dt,
                    ctx,
                    extra=extra,
                )
        except Exception as exc:
            logger.warning("[LifeScheduler] 日程生成链异常，改用本地模板：%s", exc)
            data = self._build_local_fallback_schedule(anchor_dt, ctx, extra=extra)
        finally:
            self._generating = False

        self.data_mgr.set(data)
        return data

    async def _build_context(
        self,
        anchor_dt: datetime.datetime,
        *,
        umo: str | None = None,
    ) -> ScheduleContext:
        today = anchor_dt.date()
        diversity = self._pick_diversity(today)
        persona_desc = await self._get_persona()
        recent_chats = await self._get_recent_chats(umo)
        segment_slots_text = "\n".join(
            f"- {slot['key']} | {slot['label']} | {slot['start_time']}-{slot['end_time']}"
            for slot in build_segment_slots(anchor_dt)
        )
        return ScheduleContext(
            date_str=anchor_dt.strftime("%Y-%m-%d"),
            weekday=self._weekday(anchor_dt),
            holiday=self._get_holiday_info(today),
            persona_desc=persona_desc,
            history_schedules=self._get_history(today),
            recent_chats=recent_chats,
            daily_theme=diversity["daily_theme"],
            mood_color=diversity["mood_color"],
            outfit_style=diversity["outfit_style"],
            schedule_type=diversity["schedule_type"],
            anchor_time=normalize_clock_text(str(self.config.get("schedule_time") or "07:00")),
            window_start=anchor_dt.strftime("%Y-%m-%d %H:%M"),
            window_end=(anchor_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            segment_slots_text=segment_slots_text,
        )

    async def _collect_guidance(
        self,
        anchor_dt: datetime.datetime,
        ctx: ScheduleContext,
        *,
        extra: str | None = None,
    ) -> DayGuidance:
        prompt = self._build_guidance_prompt(ctx, extra=extra)
        session_id = f"life_scheduler_gen:{anchor_dt.date().isoformat()}"
        try:
            text = await self._call_llm(prompt, sid=session_id)
        except Exception as exc:
            logger.warning("[LifeScheduler] 获取日程线索失败：%s", exc)
            return DayGuidance()
        guidance = self._parse_guidance(text)
        if not guidance.has_content():
            logger.warning("[LifeScheduler] 模型返回不可解析线索，改用本地模板。")
        return guidance

    def _build_guidance_prompt(
        self,
        ctx: ScheduleContext,
        *,
        extra: str | None = None,
    ) -> str:
        extra_line = f"\n额外要求：{extra}" if extra else ""
        return (
            "你要为一个 Bot 生成“从刷新锚点开始算 24 小时固定不变”的生活线索。\n"
            "不要返回 JSON，不要写解释，不要写代码块。\n"
            "只返回下面 6 行，每行一句短句：\n"
            "今日主线: ...\n"
            "穿搭重点: ...\n"
            "白天重点: ...\n"
            "晚间状态: ...\n"
            "自拍氛围: ...\n"
            "整体气质: ...\n\n"
            "要求：\n"
            "- 内容必须贴近日常、真实、自然、能落地。\n"
            "- 穿搭不要夸张，不要二次元，不要玄幻。\n"
            "- 日程要像普通人一天内会发生的安排。\n"
            "- 自拍氛围要像生活里顺手拍的照片。\n"
            "- 不要输出英文模板，不要输出多余前言。\n\n"
            f"日期：{ctx.date_str} {ctx.weekday}\n"
            f"节日：{ctx.holiday or '无'}\n"
            f"刷新锚点：{ctx.anchor_time}\n"
            f"固定窗口：{ctx.window_start} ~ {ctx.window_end}\n"
            f"今日主题：{ctx.daily_theme}\n"
            f"心情色彩：{ctx.mood_color}\n"
            f"穿搭风格：{ctx.outfit_style}\n"
            f"日程类型：{ctx.schedule_type}\n"
            f"时段切片：\n{ctx.segment_slots_text}\n"
            f"近期历史：\n{ctx.history_schedules}\n"
            f"近期对话：\n{ctx.recent_chats}\n"
            f"人设参考：\n{ctx.persona_desc[:1200]}"
            f"{extra_line}"
        )

    def _build_schedule_from_guidance(
        self,
        anchor_dt: datetime.datetime,
        ctx: ScheduleContext,
        guidance: DayGuidance,
        *,
        extra: str | None = None,
    ) -> ScheduleData:
        outfit_style = (ctx.outfit_style or "自然日常风").strip()
        mood = (guidance.vibe or ctx.mood_color or "平静").strip()
        outfit_focus = (guidance.outfit_focus or "舒适、利落、适合全天切换场景").strip()
        daily_hook = (guidance.daily_hook or f"今天按{ctx.daily_theme}的节奏推进日常安排").strip()
        daytime_focus = (guidance.daytime_focus or "白天把主要精力放在工作、学习或必要外出").strip()
        evening_focus = (guidance.evening_focus or "晚间逐步收尾，回到更放松的居家状态").strip()
        selfie_tone = (guidance.selfie_tone or "像生活里顺手拍下的自然自拍").strip()

        summary_outfit = (
            f"{outfit_style}，重点是{outfit_focus}，整体气质保持{mood}。"
        )
        summary_schedule = (
            f"{daily_hook} 白天以{daytime_focus}为主，晚上回到{evening_focus}。"
        )
        if extra:
            summary_schedule += f" 额外要求会体现在当天安排里：{extra}。"

        segments = build_detailed_segments(
            anchor_dt=anchor_dt,
            outfit_style=outfit_style,
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
        )
        segments = self._apply_guidance_to_segments(
            segments,
            guidance,
            outfit_focus=outfit_focus,
            mood=mood,
            selfie_tone=selfie_tone,
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
        )

    def _apply_guidance_to_segments(
        self,
        segments,
        guidance: DayGuidance,
        *,
        outfit_focus: str,
        mood: str,
        selfie_tone: str,
    ):
        for item in segments:
            if outfit_focus and outfit_focus not in item.outfit:
                item.outfit = self._merge_sentence(item.outfit, f"重点是{outfit_focus}")
            if mood:
                item.mood = self._merge_sentence(item.mood, mood)
                item.caption_hint = self._merge_sentence(item.caption_hint, f"语气保持{mood}")
            if selfie_tone:
                item.selfie_scene = self._merge_sentence(item.selfie_scene, selfie_tone)
                item.selfie_prompt_hint = self._merge_sentence(
                    item.selfie_prompt_hint,
                    f"画面要像{selfie_tone}",
                )

            if item.key in {"morning_outing", "daytime_work"} and guidance.daytime_focus:
                item.activity = self._merge_sentence(item.activity, guidance.daytime_focus)
                item.caption_hint = self._merge_sentence(item.caption_hint, guidance.daytime_focus)

            if item.key in {"after_work", "home_evening", "late_night"} and guidance.evening_focus:
                item.activity = self._merge_sentence(item.activity, guidance.evening_focus)
                item.caption_hint = self._merge_sentence(item.caption_hint, guidance.evening_focus)

            if guidance.daily_hook and item.key == "wake_up":
                item.activity = self._merge_sentence(item.activity, guidance.daily_hook)

        return segments

    def _build_local_fallback_schedule(
        self,
        anchor_dt: datetime.datetime,
        ctx: ScheduleContext | None,
        *,
        extra: str | None = None,
    ) -> ScheduleData:
        safe_ctx = ctx or ScheduleContext(
            date_str=anchor_dt.strftime("%Y-%m-%d"),
            weekday=self._weekday(anchor_dt),
            holiday="",
            persona_desc="",
            history_schedules="（无历史记录）",
            recent_chats="（无近期对话）",
            daily_theme="规律日常",
            mood_color="平静",
            outfit_style="自然日常风",
            schedule_type="规律三餐型",
            anchor_time=normalize_clock_text(str(self.config.get("schedule_time") or "07:00")),
            window_start=anchor_dt.strftime("%Y-%m-%d %H:%M"),
            window_end=(anchor_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            segment_slots_text="\n".join(
                f"- {slot['key']} | {slot['label']} | {slot['start_time']}-{slot['end_time']}"
                for slot in build_segment_slots(anchor_dt)
            ),
        )
        guidance = DayGuidance(
            daily_hook="今天按自己的节奏慢慢推进手头的事，保持真实、自然、可持续的一天。",
            outfit_focus="穿搭以舒适、利落、方便全天切换场景为主。",
            daytime_focus="白天把主要精力放在工作、学习或必要外出。",
            evening_focus="晚上逐步收尾，把状态切回更放松的居家节奏。",
            selfie_tone="像日常生活里顺手拍到的真实状态照。",
            vibe=safe_ctx.mood_color or "平静",
        )
        return self._build_schedule_from_guidance(anchor_dt, safe_ctx, guidance, extra=extra)

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
                    logger.warning("[LifeScheduler] completion 为空或命中占位回复，准备重试一次。")
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

    def _parse_guidance(self, text: str) -> DayGuidance:
        cleaned = self._sanitize_guidance_text(text)
        if not cleaned or _TOOL_PLACEHOLDER_RE.search(cleaned):
            return DayGuidance(raw_text=cleaned)

        guidance = DayGuidance(raw_text=cleaned)
        ordered_sentences: list[str] = []

        for raw_line in cleaned.splitlines():
            line = _BULLET_PREFIX_RE.sub("", raw_line.strip())
            if not line:
                continue
            match = _LABEL_LINE_RE.match(line)
            if match:
                self._assign_guidance_field(
                    guidance,
                    match.group(1).strip(),
                    match.group(2).strip(),
                )
                continue
            ordered_sentences.extend(
                sentence.strip()
                for sentence in _SENTENCE_SPLIT_RE.split(line)
                if sentence.strip()
            )

        if ordered_sentences:
            self._fill_guidance_from_sentences(guidance, ordered_sentences)
        return guidance

    def _sanitize_guidance_text(self, text: str) -> str:
        text = (text or "").strip().lstrip("\ufeff")
        if not text:
            return ""
        fence_match = _MARKDOWN_FENCE_RE.search(text)
        if fence_match:
            text = fence_match.group(1).strip()
        return text

    def _assign_guidance_field(self, guidance: DayGuidance, label: str, value: str) -> None:
        if not value:
            return
        normalized = label.strip().lower()
        mapping = {
            "今日主线": "daily_hook",
            "日程主线": "daily_hook",
            "主线安排": "daily_hook",
            "daily hook": "daily_hook",
            "穿搭重点": "outfit_focus",
            "outfit focus": "outfit_focus",
            "白天重点": "daytime_focus",
            "daytime focus": "daytime_focus",
            "晚间状态": "evening_focus",
            "夜间状态": "evening_focus",
            "evening focus": "evening_focus",
            "自拍氛围": "selfie_tone",
            "selfie tone": "selfie_tone",
            "整体气质": "vibe",
            "补充细节": "vibe",
            "vibe": "vibe",
        }
        attr = mapping.get(label) or mapping.get(normalized)
        if not attr:
            return
        current = getattr(guidance, attr)
        if current:
            return
        setattr(guidance, attr, value)

    def _fill_guidance_from_sentences(
        self,
        guidance: DayGuidance,
        sentences: list[str],
    ) -> None:
        ordered_fields = (
            "daily_hook",
            "outfit_focus",
            "daytime_focus",
            "evening_focus",
            "selfie_tone",
            "vibe",
        )
        index = 0
        for field in ordered_fields:
            if getattr(guidance, field):
                continue
            if index >= len(sentences):
                break
            setattr(guidance, field, sentences[index])
            index += 1

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
            return "自然日常风"

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
            if style:
                used.add(style)

        candidates = [style for style in styles if style not in used]
        return random.choice(candidates or styles)

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
            style = (getattr(data, "outfit_style", "") or "").strip()
            summary_outfit = (data.summary_outfit or data.outfit or "")[:60]
            summary_schedule = (data.summary_schedule or data.schedule or "")[:80]
            items.append(
                f"[{hist_date.strftime('%Y-%m-%d')}] 风格：{style}；穿搭：{summary_outfit}；安排：{summary_schedule}"
            )
        return "\n".join(items) if items else "（无历史记录）"

    async def _get_recent_chats(
        self,
        umo: str | None = None,
        count: int | None = None,
    ) -> str:
        count = count or self.config["reference_recent_count"]
        if not umo or not count:
            return "（无近期对话）"

        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return "（无近期对话）"
            conv = await self.context.conversation_manager.get_conversation(umo, cid)
            if not conv or not conv.history:
                return "（无近期对话）"
            history = json.loads(conv.history)
            recent = history[-count:] if count > 0 else []

            formatted: list[str] = []
            for msg in recent:
                role = msg.get("role", "unknown")
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                if role == "user":
                    formatted.append(f"用户：{content}")
                elif role == "assistant":
                    formatted.append(f"Bot：{content}")
            return "\n".join(formatted) if formatted else "（无近期对话）"
        except Exception as exc:
            logger.error("Failed to get recent chats for %s: %s", umo, exc)
            return "（获取对话记录失败）"

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

    @staticmethod
    def _merge_sentence(base: str, extra: str) -> str:
        base = (base or "").strip()
        extra = (extra or "").strip()
        if not extra:
            return base
        if not base:
            return extra
        if extra in base:
            return base
        base = base.rstrip("。；;，, ")
        extra = extra.rstrip("。；;，, ")
        return f"{base}，{extra}。"
