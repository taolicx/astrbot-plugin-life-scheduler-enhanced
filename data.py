from __future__ import annotations

import datetime
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal, Tuple, Union


def compat_dataclass(*args, **kwargs):
    """兼容较老本机 Python，对 slots 形参做降级处理。"""
    if sys.version_info < (3, 10):
        kwargs = dict(kwargs)
        kwargs.pop("slots", None)
    return dataclass(*args, **kwargs)

ScheduleStatus = Literal["ok", "failed"]

DateLike = Union[
    datetime.datetime,
    datetime.date,
    int,
    float,
]

SEGMENT_BLUEPRINTS: Tuple[Tuple[str, str, int, int], ...] = (
    ("wake_up", "起床后在家", 0, 90),
    ("morning_outing", "出门通勤", 90, 240),
    ("daytime_work", "白天工作/学习", 240, 660),
    ("after_work", "下班返程", 660, 780),
    ("home_evening", "到家后放松", 780, 1020),
    ("late_night", "夜间休息", 1020, 1440),
)


def parse_clock_text(clock_text: str) -> tuple[int, int]:
    parts = [part.strip() for part in str(clock_text or "").split(":")]
    if len(parts) < 2:
        raise ValueError(f"Invalid clock text: {clock_text}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid clock text: {clock_text}")
    return hour, minute


def normalize_clock_text(clock_text: str) -> str:
    hour, minute = parse_clock_text(clock_text)
    return f"{hour:02d}:{minute:02d}"


def _to_datetime(value: DateLike) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value)
    raise TypeError(f"Unsupported date type: {type(value)}")


def resolve_cycle_anchor(value: DateLike, anchor_time: str = "07:00") -> datetime.datetime:
    moment = _to_datetime(value)
    anchor_clock = normalize_clock_text(anchor_time)
    hour, minute = parse_clock_text(anchor_clock)
    anchor = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment < anchor:
        anchor -= datetime.timedelta(days=1)
    return anchor


def to_anchor_date_str(value: DateLike, anchor_time: str = "07:00") -> str:
    return resolve_cycle_anchor(value, anchor_time).date().isoformat()


def format_clock(moment: datetime.datetime) -> str:
    return moment.strftime("%H:%M")


def build_segment_slots(anchor_dt: datetime.datetime) -> list[dict[str, str]]:
    slots: list[dict[str, str]] = []
    for key, label, start_offset, end_offset in SEGMENT_BLUEPRINTS:
        start_dt = anchor_dt + datetime.timedelta(minutes=start_offset)
        end_dt = anchor_dt + datetime.timedelta(minutes=end_offset)
        slots.append(
            {
                "key": key,
                "label": label,
                "start_time": format_clock(start_dt),
                "end_time": format_clock(end_dt),
            }
        )
    return slots


def resolve_clock_in_window(
    clock_text: str,
    *,
    window_start: datetime.datetime,
) -> datetime.datetime:
    hour, minute = parse_clock_text(clock_text)
    candidate = window_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < window_start:
        candidate += datetime.timedelta(days=1)
    return candidate


def _join_nonempty(parts: list[str]) -> str:
    return "，".join(
        [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    )


@compat_dataclass(slots=True)
class ScheduleSegment:
    key: str
    label: str = ""
    start_time: str = ""
    end_time: str = ""
    outfit: str = ""
    outfit_top: str = ""
    outfit_bottom: str = ""
    outfit_outerwear: str = ""
    outfit_shoes: str = ""
    outfit_accessories: str = ""
    hairstyle: str = ""
    makeup: str = ""
    activity: str = ""
    location: str = ""
    mood: str = ""
    selfie_scene: str = ""
    selfie_pose: str = ""
    selfie_lighting: str = ""
    selfie_prompt_hint: str = ""
    caption_hint: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleSegment":
        return cls(
            key=str(data.get("key") or "").strip(),
            label=str(data.get("label") or "").strip(),
            start_time=str(data.get("start_time") or "").strip(),
            end_time=str(data.get("end_time") or "").strip(),
            outfit=str(data.get("outfit") or "").strip(),
            outfit_top=str(data.get("outfit_top") or "").strip(),
            outfit_bottom=str(data.get("outfit_bottom") or "").strip(),
            outfit_outerwear=str(data.get("outfit_outerwear") or "").strip(),
            outfit_shoes=str(data.get("outfit_shoes") or "").strip(),
            outfit_accessories=str(data.get("outfit_accessories") or "").strip(),
            hairstyle=str(data.get("hairstyle") or "").strip(),
            makeup=str(data.get("makeup") or "").strip(),
            activity=str(data.get("activity") or "").strip(),
            location=str(data.get("location") or "").strip(),
            mood=str(data.get("mood") or "").strip(),
            selfie_scene=str(data.get("selfie_scene") or "").strip(),
            selfie_pose=str(data.get("selfie_pose") or "").strip(),
            selfie_lighting=str(data.get("selfie_lighting") or "").strip(),
            selfie_prompt_hint=str(data.get("selfie_prompt_hint") or "").strip(),
            caption_hint=str(data.get("caption_hint") or "").strip(),
        )

    def contains(self, moment: datetime.datetime, *, window_start: datetime.datetime) -> bool:
        start_dt = resolve_clock_in_window(self.start_time, window_start=window_start)
        end_dt = resolve_clock_in_window(self.end_time, window_start=window_start)
        if end_dt <= start_dt:
            end_dt += datetime.timedelta(days=1)
        return start_dt <= moment < end_dt

    def outfit_detail_text(self) -> str:
        detailed = _join_nonempty(
            [
                f"上装{self.outfit_top}" if self.outfit_top else "",
                f"下装{self.outfit_bottom}" if self.outfit_bottom else "",
                f"外搭{self.outfit_outerwear}" if self.outfit_outerwear else "",
                f"鞋履{self.outfit_shoes}" if self.outfit_shoes else "",
                f"配饰{self.outfit_accessories}" if self.outfit_accessories else "",
            ]
        )
        if self.outfit and detailed:
            return f"{self.outfit}；细节包括{detailed}"
        return self.outfit or detailed

    def selfie_visual_text(self) -> str:
        return _join_nonempty(
            [
                f"发型{self.hairstyle}" if self.hairstyle else "",
                f"妆面{self.makeup}" if self.makeup else "",
                f"姿态{self.selfie_pose}" if self.selfie_pose else "",
                f"光线{self.selfie_lighting}" if self.selfie_lighting else "",
            ]
        )


def build_default_segments(
    *,
    anchor_dt: datetime.datetime,
    outfit_style: str,
    summary_outfit: str,
    summary_schedule: str,
) -> list[ScheduleSegment]:
    segment_defaults = {
        "wake_up": {
            "activity": "刚起床，在家里慢慢清醒、洗漱和整理状态。",
            "location": "家里",
            "mood": "松弛、清醒",
            "selfie_scene": "刚整理好状态，在家里自然随手自拍",
            "selfie_prompt_hint": "保留居家晨间感，妆容轻，光线柔和，像刚起床后整理好自己随手拍的照片。",
            "caption_hint": "像刚起床后安静记录状态。",
        },
        "morning_outing": {
            "activity": "准备出门，切换到工作或外出状态，节奏更利落。",
            "location": "出门路上",
            "mood": "清醒、利落",
            "selfie_scene": "出门前或通勤途中带一点赶时间感的自拍",
            "selfie_prompt_hint": "突出出门穿搭完整度，适合通勤、上班、出门办事前的真实自拍。",
            "caption_hint": "像出门前顺手拍一张。",
        },
        "daytime_work": {
            "activity": "白天以工作、学习、见人或处理事务为主。",
            "location": "办公室、学校或外出场所",
            "mood": "专注、稳定",
            "selfie_scene": "白天工作间隙自然记录一下当下状态",
            "selfie_prompt_hint": "像白天工作或学习间隙拍的生活照，穿搭完整，表情自然。",
            "caption_hint": "像白天忙里偷闲拍一张。",
        },
        "after_work": {
            "activity": "处理完白天主要事务，开始回程或晚间外出。",
            "location": "回家路上或傍晚街头",
            "mood": "放松下来、略带疲惫",
            "selfie_scene": "傍晚回程时的生活自拍",
            "selfie_prompt_hint": "有下班后松一口气的感觉，光线偏傍晚，生活感强。",
            "caption_hint": "像傍晚收工后的状态。",
        },
        "home_evening": {
            "activity": "回到家后放松、吃饭、整理和安排自己的时间。",
            "location": "家里",
            "mood": "舒缓、温和",
            "selfie_scene": "回家后换上更舒服的状态，在家里自拍",
            "selfie_prompt_hint": "强调回到家后的舒适感和轻松感，像晚饭后随手自拍。",
            "caption_hint": "像晚上回到家终于松下来。",
        },
        "late_night": {
            "activity": "夜里逐渐收尾，准备休息或安静做自己的事。",
            "location": "家里",
            "mood": "安静、慵懒",
            "selfie_scene": "夜里睡前安静记录一下自己",
            "selfie_prompt_hint": "夜间氛围更柔和安静，像睡前在房间里随手自拍。",
            "caption_hint": "像睡前记录今天的尾声。",
        },
    }

    segments: list[ScheduleSegment] = []
    for slot in build_segment_slots(anchor_dt):
        meta = segment_defaults.get(slot["key"], {})
        segments.append(
            ScheduleSegment(
                key=slot["key"],
                label=slot["label"],
                start_time=slot["start_time"],
                end_time=slot["end_time"],
                outfit=summary_outfit,
                activity=str(meta.get("activity") or summary_schedule).strip(),
                location=str(meta.get("location") or "日常活动场景").strip(),
                mood=str(meta.get("mood") or "自然").strip(),
                selfie_scene=str(meta.get("selfie_scene") or "自然生活自拍").strip(),
                selfie_prompt_hint=str(meta.get("selfie_prompt_hint") or "").strip(),
                caption_hint=str(meta.get("caption_hint") or "").strip(),
            )
        )
    return segments


def build_detailed_segments(
    *,
    anchor_dt: datetime.datetime,
    outfit_style: str,
    summary_outfit: str,
    summary_schedule: str,
) -> list[ScheduleSegment]:
    segments = build_default_segments(
        anchor_dt=anchor_dt,
        outfit_style=outfit_style,
        summary_outfit=summary_outfit,
        summary_schedule=summary_schedule,
    )
    detail_map: dict[str, dict[str, str]] = {
        "wake_up": {
            "outfit": f"{outfit_style} 的晨起居家穿搭，轻薄、软乎、清爽，像刚洗漱完后的自然少女状态。",
            "outfit_top": "柔软短T、薄家居背心外搭轻薄开衫或宽松棉质上衣",
            "outfit_bottom": "家居短裤、软糯长裤或宽松居家短裙",
            "outfit_outerwear": "薄开衫、防晒小披肩或不额外加外套",
            "outfit_shoes": "软底拖鞋",
            "outfit_accessories": "发圈、小耳钉或可爱素色手机壳",
            "hairstyle": "自然披发、松松低丸子头或随手扎起的低马尾",
            "makeup": "近素颜或只有轻薄底妆、淡唇色和一点点气色",
            "activity": "起床后在家慢慢清醒，洗漱、整理房间、换上第一套舒适衣服。",
            "location": "卧室、洗漱台、窗边或家里镜前",
            "mood": "安静、清醒、软软的，还带一点刚醒来的松弛感",
            "selfie_scene": "刚洗漱完或整理好头发后，在家里随手自拍一张。",
            "selfie_pose": "单手举手机，对镜或半侧身站在窗边",
            "selfie_lighting": "晨间自然窗光，柔和偏亮",
            "selfie_prompt_hint": "保留晨起后的居家感和轻微松弛感，强调清透、年轻、真实，不要做成成熟棚拍。",
            "caption_hint": "像刚起床整理好自己后，安静记录一下今天的开场。",
        },
        "morning_outing": {
            "outfit": f"{outfit_style} 的清爽出门穿搭，适合通学、实习、上午外出或见朋友。",
            "outfit_top": "短袖针织、娃娃领上衣、合身T恤或干净衬衫",
            "outfit_bottom": "牛仔裤、百褶裙、A字短裙或轻薄长裤",
            "outfit_outerwear": "防晒衫、短款开衫、轻薄外套或不额外加厚外套",
            "outfit_shoes": "帆布鞋、干净运动鞋、玛丽珍鞋或乐福鞋",
            "outfit_accessories": "帆布包、小挂件、细项链或小耳饰",
            "hairstyle": "整理过的低马尾、披发、半扎发或高马尾",
            "makeup": "清透淡妆，气色干净，整体显年轻",
            "activity": "准备出门，切换到工作或办事节奏，赶在上午把状态提起来。",
            "location": "玄关、电梯口、楼下或通勤路上",
            "mood": "清醒、利落、带一点赶时间的轻快",
            "selfie_scene": "出门前对镜或通勤途中顺手拍一张，重点是完整穿搭。",
            "selfie_pose": "拎包站姿、肩侧构图或边走边回头看镜头",
            "selfie_lighting": "早晨偏亮自然光，户外或走廊光线",
            "selfie_prompt_hint": "突出出门前的完整穿搭和年轻清爽状态，像真的在玄关或路上顺手自拍，不要做成熟职场照。",
            "caption_hint": "像出门前顺手留一张，整个人已经进入白天节奏。",
        },
        "daytime_work": {
            "outfit": f"{outfit_style} 的白天主线穿搭，完整、清爽、显年轻，是今天最稳定的一套外出状态。",
            "outfit_top": "清爽上衣、短袖针织、干净衬衫或学院感内搭",
            "outfit_bottom": "牛仔裤、短裙、半裙或轻便裤装",
            "outfit_outerwear": "针织外搭、薄外套、防晒衫或轻风衣",
            "outfit_shoes": "适合久站和行走的球鞋、帆布鞋或轻便平底鞋",
            "outfit_accessories": "耳饰、发夹、包或简单戒指",
            "hairstyle": "白天维持整洁又轻盈的发型，发丝自然干净",
            "makeup": "白天清透淡妆，底妆薄，眼妆和唇色都偏自然",
            "activity": "白天以学习、轻办公、见人、沟通和处理事务为主，节奏持续在线。",
            "location": "自习区、学校、工位、咖啡店或白天外出场所",
            "mood": "专注、稳定、清醒但不紧绷，整体很有少女日常感",
            "selfie_scene": "白天忙里偷闲记录一下状态，像在自习区、洗手间镜子或咖啡店顺手拍的自拍。",
            "selfie_pose": "半身镜前自拍、坐姿举手机或靠墙侧拍",
            "selfie_lighting": "室内均匀白光或窗边自然光",
            "selfie_prompt_hint": "保留白天主线穿搭和清醒状态，画面要像忙里偷闲拍一张，强调年轻、自然、显精神。",
            "caption_hint": "像白天忙里偷闲拍一张，顺手记一下今天的状态。",
        },
        "after_work": {
            "outfit": f"{outfit_style} 的下班返程穿搭，还是白天那套主线，但状态更松一点。",
            "outfit_top": "白天主线内搭保持不变，领口或袖口略微放松",
            "outfit_bottom": "维持白天下装",
            "outfit_outerwear": "外套敞开穿、半披着或直接搭在手臂上",
            "outfit_shoes": "仍是白天鞋履，但呈现一点走了一天后的真实感",
            "outfit_accessories": "肩背包、手拿咖啡或下班路上的小物",
            "hairstyle": "发型略有松动，但整体还整洁",
            "makeup": "妆面保持着，但比早上更自然",
            "activity": "结束白天事务，开始回程、顺路买东西或转入晚间安排。",
            "location": "地铁、电梯、街边、车里或傍晚街头",
            "mood": "放松下来，带一点疲惫和收工感",
            "selfie_scene": "下班后在返程路上、商场玻璃前或电梯里随手拍一张。",
            "selfie_pose": "单手拿手机、另一手拎包或扶电梯镜面侧拍",
            "selfie_lighting": "傍晚街灯、商场灯光或车内柔和光线",
            "selfie_prompt_hint": "强化下班后的松一口气和傍晚生活感，穿搭仍然是白天那套，但状态更放松。",
            "caption_hint": "像下班返程时顺手拍的一张，带一点收工后的轻松。",
        },
        "home_evening": {
            "outfit": f"{outfit_style} 的到家后放松穿搭，明显比白天更舒服、更居家。",
            "outfit_top": "宽松家居上衣、柔软针织或舒适T恤",
            "outfit_bottom": "运动短裤、针织长裤或家居裙",
            "outfit_outerwear": "薄针织开衫、家居外披、防晒薄衫或不再加外套",
            "outfit_shoes": "拖鞋或赤脚在家",
            "outfit_accessories": "居家发夹、细发圈、小耳饰或不刻意戴配饰",
            "hairstyle": "回家后松开的头发、鲨鱼夹盘发、低丸子头或松松侧扎",
            "makeup": "妆面减淡，像回家后只留下清透底妆和自然气色",
            "activity": "回到家后吃饭、洗漱、整理东西、做自己的晚间安排。",
            "location": "客厅、厨房、卧室或镜前",
            "mood": "舒缓、温和、终于彻底放松，还带一点软乎乎的居家少女感",
            "selfie_scene": "晚饭后、洗完澡前后或在家换完衣服后随手自拍。",
            "selfie_pose": "坐在沙发边、镜前站姿或半躺着举手机",
            "selfie_lighting": "室内暖光或夜间台灯光，偏柔和",
            "selfie_prompt_hint": "突出回家后的舒适感和松弛感，允许更轻薄清凉一点，但仍然要自然、不擦边，整体显年轻。",
            "caption_hint": "像晚上回到家终于慢下来，随手记录一下当下的状态。",
        },
        "late_night": {
            "outfit": f"{outfit_style} 的夜间收尾穿搭，进入睡前状态，更轻、更软、更安静，也更像自然少女居家。",
            "outfit_top": "柔软睡衣上衣、吊带外搭薄衫、短T或宽松棉质上衣",
            "outfit_bottom": "睡裤、家居短裤、软糯长裤或轻薄长裙",
            "outfit_outerwear": "薄披肩、轻薄针织或不再额外加外搭",
            "outfit_shoes": "拖鞋或不穿鞋",
            "outfit_accessories": "细发圈、发夹、小耳钉或几乎无配饰",
            "hairstyle": "松散长发、睡前低辫、松松低丸子头或简单夹起",
            "makeup": "淡到几乎看不出，只保留自然气色和一点点清透感",
            "activity": "夜里把白天彻底收尾，准备休息，或安静做一点自己的事再睡。",
            "location": "卧室、床边、窗帘旁或夜间镜前",
            "mood": "安静、慵懒、轻柔，带一点睡前的私密感",
            "selfie_scene": "睡前在房间里安静地举手机自拍一张，像记录今天的尾声。",
            "selfie_pose": "靠床坐着、镜前低角度自拍或半侧脸看向镜头",
            "selfie_lighting": "夜间暖色台灯或昏柔室内光",
            "selfie_prompt_hint": "保持夜间安静氛围和睡前松弛感，画面柔和、真实、显年轻，不要做成成熟夜拍或过度修饰。",
            "caption_hint": "像睡前想留一张今天最后的状态，语气更安静一点。",
        },
    }

    enriched: list[ScheduleSegment] = []
    for segment in segments:
        detail = detail_map.get(segment.key, {})
        base_outfit = str(detail.get("outfit") or segment.outfit or summary_outfit).strip()
        enriched.append(
            ScheduleSegment(
                key=segment.key,
                label=segment.label,
                start_time=segment.start_time,
                end_time=segment.end_time,
                outfit=base_outfit,
                outfit_top=str(detail.get("outfit_top") or "").strip(),
                outfit_bottom=str(detail.get("outfit_bottom") or "").strip(),
                outfit_outerwear=str(detail.get("outfit_outerwear") or "").strip(),
                outfit_shoes=str(detail.get("outfit_shoes") or "").strip(),
                outfit_accessories=str(detail.get("outfit_accessories") or "").strip(),
                hairstyle=str(detail.get("hairstyle") or "").strip(),
                makeup=str(detail.get("makeup") or "").strip(),
                activity=str(detail.get("activity") or segment.activity or summary_schedule).strip(),
                location=str(detail.get("location") or segment.location).strip(),
                mood=str(detail.get("mood") or segment.mood).strip(),
                selfie_scene=str(detail.get("selfie_scene") or segment.selfie_scene).strip(),
                selfie_pose=str(detail.get("selfie_pose") or "").strip(),
                selfie_lighting=str(detail.get("selfie_lighting") or "").strip(),
                selfie_prompt_hint=str(detail.get("selfie_prompt_hint") or segment.selfie_prompt_hint).strip(),
                caption_hint=str(detail.get("caption_hint") or segment.caption_hint).strip(),
            )
        )
    return enriched


def hydrate_segments_with_defaults(
    *,
    anchor_dt: datetime.datetime,
    outfit_style: str,
    summary_outfit: str,
    summary_schedule: str,
    segments: list[ScheduleSegment],
) -> list[ScheduleSegment]:
    default_map = {
        segment.key: segment
        for segment in build_detailed_segments(
            anchor_dt=anchor_dt,
            outfit_style=outfit_style,
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
        )
    }
    hydrated: list[ScheduleSegment] = []
    for segment in segments:
        default = default_map.get(segment.key)
        if default is None:
            hydrated.append(segment)
            continue
        hydrated.append(
            ScheduleSegment(
                key=segment.key,
                label=segment.label or default.label,
                start_time=segment.start_time or default.start_time,
                end_time=segment.end_time or default.end_time,
                outfit=segment.outfit or default.outfit,
                outfit_top=segment.outfit_top or default.outfit_top,
                outfit_bottom=segment.outfit_bottom or default.outfit_bottom,
                outfit_outerwear=segment.outfit_outerwear or default.outfit_outerwear,
                outfit_shoes=segment.outfit_shoes or default.outfit_shoes,
                outfit_accessories=segment.outfit_accessories or default.outfit_accessories,
                hairstyle=segment.hairstyle or default.hairstyle,
                makeup=segment.makeup or default.makeup,
                activity=segment.activity or default.activity,
                location=segment.location or default.location,
                mood=segment.mood or default.mood,
                selfie_scene=segment.selfie_scene or default.selfie_scene,
                selfie_pose=segment.selfie_pose or default.selfie_pose,
                selfie_lighting=segment.selfie_lighting or default.selfie_lighting,
                selfie_prompt_hint=segment.selfie_prompt_hint or default.selfie_prompt_hint,
                caption_hint=segment.caption_hint or default.caption_hint,
            )
        )
    return hydrated


@compat_dataclass(slots=True)
class ScheduleData:
    date: str
    anchor_time: str = "07:00"
    window_start: str = ""
    window_end: str = ""
    outfit_style: str = ""
    outfit: str = ""
    schedule: str = ""
    summary_outfit: str = ""
    summary_schedule: str = ""
    segments: list[ScheduleSegment] = field(default_factory=list)
    status: ScheduleStatus = "ok"

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleData":
        raw_segments = data.get("segments") or []
        segments: list[ScheduleSegment] = []
        if isinstance(raw_segments, list):
            for item in raw_segments:
                if isinstance(item, dict):
                    try:
                        segments.append(ScheduleSegment.from_dict(item))
                    except Exception:
                        continue

        summary_outfit = str(data.get("summary_outfit") or data.get("outfit") or "").strip()
        summary_schedule = str(data.get("summary_schedule") or data.get("schedule") or "").strip()
        anchor_time = normalize_clock_text(str(data.get("anchor_time") or "07:00"))
        window_start = str(data.get("window_start") or "").strip()
        window_end = str(data.get("window_end") or "").strip()
        record = cls(
            date=data["date"],
            anchor_time=anchor_time,
            window_start=window_start,
            window_end=window_end,
            outfit_style=str(data.get("outfit_style") or "").strip(),
            outfit=str(data.get("outfit") or summary_outfit).strip(),
            schedule=str(data.get("schedule") or summary_schedule).strip(),
            summary_outfit=summary_outfit,
            summary_schedule=summary_schedule,
            segments=segments,
            status=data.get("status", "ok"),
        )
        return record.with_defaults()

    def with_defaults(self) -> "ScheduleData":
        anchor_dt = self.window_start_dt
        if anchor_dt is None:
            anchor_dt = resolve_cycle_anchor(
                datetime.datetime.fromisoformat(f"{self.date}T{self.anchor_time}:00"),
                self.anchor_time,
            )
            self.window_start = anchor_dt.isoformat(timespec="seconds")
            self.window_end = (anchor_dt + datetime.timedelta(days=1)).isoformat(
                timespec="seconds"
            )

        if not self.summary_outfit:
            self.summary_outfit = self.outfit
        if not self.summary_schedule:
            self.summary_schedule = self.schedule
        if not self.outfit:
            self.outfit = self.summary_outfit
        if not self.schedule:
            self.schedule = self.summary_schedule
        default_outfit_style = self.outfit_style or "自然日常风"
        default_summary_outfit = self.summary_outfit or self.outfit or "自然舒服的日常穿搭"
        default_summary_schedule = self.summary_schedule or self.schedule or "按自己的节奏安排一天"
        if not self.segments:
            self.segments = build_detailed_segments(
                anchor_dt=anchor_dt,
                outfit_style=self.outfit_style or "自然日常风",
                summary_outfit=self.summary_outfit or self.outfit or "自然舒服的日常穿搭",
                summary_schedule=self.summary_schedule or self.schedule or "按自己的节奏安排一天",
            )
        else:
            self.segments = hydrate_segments_with_defaults(
                anchor_dt=anchor_dt,
                outfit_style=default_outfit_style,
                summary_outfit=default_summary_outfit,
                summary_schedule=default_summary_schedule,
                segments=self.segments,
            )
        return self

    @property
    def window_start_dt(self) -> datetime.datetime | None:
        if not self.window_start:
            return None
        try:
            return datetime.datetime.fromisoformat(self.window_start)
        except ValueError:
            return None

    @property
    def window_end_dt(self) -> datetime.datetime | None:
        if not self.window_end:
            return None
        try:
            return datetime.datetime.fromisoformat(self.window_end)
        except ValueError:
            return None

    def active_segment(self, moment: datetime.datetime | None = None) -> ScheduleSegment | None:
        if not self.segments:
            return None
        moment = moment or datetime.datetime.now()
        window_start = self.window_start_dt
        if window_start is None:
            return self.segments[0]
        for segment in self.segments:
            if segment.contains(moment, window_start=window_start):
                return segment
        return self.segments[-1]


class ScheduleDataManager:
    def __init__(
        self,
        json_path: Path,
        anchor_time_provider: Callable[[], str] | None = None,
    ):
        self._path = json_path
        self._data: dict[str, ScheduleData] = {}
        self._anchor_time_provider = anchor_time_provider or (lambda: "07:00")
        self.load()

    def _current_anchor_time(self) -> str:
        try:
            return normalize_clock_text(str(self._anchor_time_provider() or "07:00"))
        except Exception:
            return "07:00"

    def has(self, date: DateLike) -> bool:
        return to_anchor_date_str(date, self._current_anchor_time()) in self._data

    def get(self, date: DateLike) -> ScheduleData | None:
        return self._data.get(to_anchor_date_str(date, self._current_anchor_time()))

    def get_exact(self, date_key: str) -> ScheduleData | None:
        return self._data.get(date_key)

    def latest(self) -> ScheduleData | None:
        if not self._data:
            return None
        latest_key = sorted(self._data.keys())[-1]
        return self._data.get(latest_key)

    def set(self, data: ScheduleData) -> None:
        self._data[data.date] = data.with_defaults()
        self.save()

    def remove(self, date: DateLike) -> None:
        if self._data.pop(to_anchor_date_str(date, self._current_anchor_time()), None):
            self.save()

    def all(self) -> dict[str, ScheduleData]:
        return dict(self._data)

    def load(self) -> None:
        if not self._path.exists():
            self._data.clear()
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._data.clear()
            return

        data: dict[str, ScheduleData] = {}
        for date_str, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                parsed = ScheduleData.from_dict(item)
            except Exception:
                continue
            data[date_str] = parsed
        self._data = data

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        payload = {date: asdict(data.with_defaults()) for date, data in self._data.items()}
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    def clear(self, *, save: bool = True) -> None:
        self._data.clear()
        if save:
            self.save()
