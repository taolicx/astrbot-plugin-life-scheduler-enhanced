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

from .data import ScheduleData, ScheduleDataManager

_STYLE_PREFIX_RE = re.compile(
    r"^\s*(?:风格|【风格】|\[风格\])\s*[:：]\s*(?P<style>.+?)(?:\n|$)"
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_KEY_VALUE_LINE_RE = re.compile(r"^\s*(outfit_style|outfit|schedule)\s*[:：]\s*(.*)$")


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
        # 生成入口需要串行化，避免同一天日程被多个请求同时写坏。
        async with self._gen_lock:
            if self._generating:
                raise RuntimeError("schedule_generating")
            self._generating = True

        data: ScheduleData | None = None
        date = date or datetime.datetime.now()
        date_str = date.strftime("%Y-%m-%d")
        try:
            logger.info(f"正在生成 {date_str} 的日程...")
            ctx = await self._collect_context(date, umo)
            prompt = self._build_prompt(ctx, extra)
            sid_base = f"life_scheduler_gen_{date_str}"
            content = await self._call_llm(prompt, sid=f"{sid_base}_0")

            # 这里先做宽松解析，再做风格约束校验，尽量兼容不稳定的模型输出。
            payload = self._extract_json_obj(content)
            ok, reason = self._validate_payload(payload, ctx)
            for attempt in range(1, self._STYLE_ENFORCE_RETRIES + 1):
                if ok:
                    break
                repair_prompt = self._build_style_repair_prompt(ctx, content, reason)
                content = await self._call_llm(repair_prompt, sid=f"{sid_base}_{attempt}")
                payload = self._extract_json_obj(content)
                ok, reason = self._validate_payload(payload, ctx)

            if not ok or not payload:
                raise ValueError(f"模型未遵循穿搭风格约束：{reason}")

            data = self._to_schedule_data(payload, date_str, ctx)
            self.data_mgr.set(data)
            logger.info(
                f"日程生成成功: {json.dumps(asdict(data), ensure_ascii=False, indent=2)}"
            )
            return data
        except Exception as e:
            logger.error(f"日程生成失败: {e}")
            return ScheduleData(
                date=date_str,
                outfit="生成失败",
                schedule="生成失败",
                status="failed",
            )
        finally:
            async with self._gen_lock:
                self._generating = False
            if data:
                self.data_mgr.set(data)

    async def _collect_context(
        self,
        date: datetime.datetime,
        umo: str | None,
    ) -> ScheduleContext:
        return ScheduleContext(
            date_str=date.strftime("%Y年%m月%d日"),
            weekday=self._weekday(date),
            holiday=self._get_holiday_info(date.date()),
            persona_desc=await self._get_persona(),
            history_schedules=self._get_history(date.date()),
            recent_chats=await self._get_recent_chats(umo),
            **self._pick_diversity(date.date()),
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

            outfit = data.outfit[:40]
            schedule = data.schedule[:60]
            style = (
                (getattr(data, "outfit_style", "") or "").strip()
                or self._extract_style_from_outfit(data.outfit)
            )

            if style:
                items.append(
                    f"[{hist_date.strftime('%Y-%m-%d')}] 风格：{style} 穿搭：{outfit} 日程：{schedule}"
                )
            else:
                items.append(
                    f"[{hist_date.strftime('%Y-%m-%d')}] 穿搭：{outfit} 日程：{schedule}"
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
        except Exception as e:
            logger.error(f"Failed to get recent chats for {umo}: {e}")
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
        tmpl_vars = set(re.findall(r"\{(\w+)\}", self.config["prompt_template"]))
        missing = tmpl_vars - ctx_dict.keys()
        if missing:
            logger.warning(
                "prompt 模板存在 ScheduleContext 未提供的字段：%s | 已自动替换成空串",
                missing,
            )

        for key in missing:
            ctx_dict[key] = ""

        prompt = self.config["prompt_template"].format(**ctx_dict)

        if ctx.outfit_style:
            prompt += (
                "\n\n## 强制约束（必须严格遵循）\n"
                f"- 你必须严格遵循穿搭风格：【{ctx.outfit_style}】（不得替换或混用其他风格）。\n"
                "- 你必须只输出 JSON 对象本体，不要 Markdown，不要代码块，不要解释。\n"
                f'- JSON 必须包含字段 "outfit_style"，且其值必须严格等于 "{ctx.outfit_style}"。\n'
                f'- 字段 "outfit" 的第一行必须以 "风格：{ctx.outfit_style}" 开头。\n'
            )

        if extra:
            prompt += f"\n\n【用户补充要求】\n请在生成日程时特别注意以下要求：{extra}"

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
                if text:
                    return text
                if attempt < self._EMPTY_COMPLETION_RETRIES:
                    logger.warning("LLM completion 为空，准备重试一次")
            raise RuntimeError("API 返回的 completion 为空")
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

    def _extract_json_obj(self, text: str) -> dict[str, str] | None:
        # 模型经常返回 fenced json、伪 JSON、甚至 key-value 文本，这里按宽松顺序兜底提取。
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

    def _try_parse_payload(self, candidate: str) -> dict[str, str] | None:
        # 先尝试标准 JSON，再尝试 literal_eval 和轻量纠偏，降低模型格式跑偏导致的失败率。
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

    def _extract_key_value_payload(self, text: str) -> dict[str, str] | None:
        text = (text or "").strip()
        if not text:
            return None

        data: dict[str, str] = {}
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
                previous = data.get(current_key, "")
                data[current_key] = (
                    (previous + "\n" + line).strip() if previous else line
                )

        if not any(data.get(key) for key in ("outfit_style", "outfit", "schedule")):
            return None

        return self._coerce_payload(data)

    def _coerce_payload(self, data: dict[str, Any]) -> dict[str, str]:
        return {
            "outfit_style": str(data.get("outfit_style", "")).strip(),
            "outfit": str(data.get("outfit", "")).strip(),
            "schedule": str(data.get("schedule", "")).strip(),
        }

    def _validate_payload(
        self,
        payload: dict[str, str] | None,
        ctx: ScheduleContext,
    ) -> tuple[bool, str]:
        if not payload:
            return False, "未能解析出 JSON 对象"

        outfit = str(payload.get("outfit", "")).strip()
        schedule = str(payload.get("schedule", "")).strip()
        if not outfit:
            return False, "outfit 不能为空"
        if not schedule:
            return False, "schedule 不能为空"

        required = (ctx.outfit_style or "").strip()
        if not required:
            return True, ""

        model_style = str(payload.get("outfit_style", "")).strip()
        if model_style != required:
            return False, f'outfit_style 必须严格等于 "{required}"'

        if not re.match(
            rf"^\s*(?:风格|【风格】|\[风格\])\s*[:：]\s*{re.escape(required)}(?:\s|$)",
            outfit,
        ):
            return False, f'outfit 第一行必须以 "风格：{required}" 开头'

        return True, ""

    def _build_style_repair_prompt(
        self,
        ctx: ScheduleContext,
        bad_text: str,
        reason: str,
    ) -> str:
        required = (ctx.outfit_style or "").strip()
        return (
            "你之前的输出未通过校验，需要按要求重写。\n"
            f"校验原因：{reason}\n"
            f"必须使用穿搭风格：{required}\n\n"
            "请只输出 JSON 对象本体，不要 Markdown，不要解释。\n"
            '输出 JSON 必须包含字段：outfit_style、outfit、schedule。\n'
            f'其中 outfit_style 必须严格等于 "{required}"，'
            f'outfit 第一行必须以 "风格：{required}" 开头。\n\n'
            "你之前的输出（供参考，可能不合规）：\n"
            f"{bad_text}\n"
        )

    def _to_schedule_data(
        self,
        payload: dict[str, str],
        date_str: str,
        ctx: ScheduleContext,
    ) -> ScheduleData:
        outfit = str(payload.get("outfit", "")).strip() or "日常休闲装"
        schedule = str(payload.get("schedule", "")).strip() or "自由安排的一天"
        outfit_style = (
            str(payload.get("outfit_style", "")).strip() or (ctx.outfit_style or "")
        )
        return ScheduleData(
            date=date_str,
            outfit_style=outfit_style,
            outfit=outfit,
            schedule=schedule,
        )
