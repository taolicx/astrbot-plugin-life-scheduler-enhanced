import datetime
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal, Tuple, Union

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


@dataclass(slots=True)
class ScheduleSegment:
    key: str
    label: str = ""
    start_time: str = ""
    end_time: str = ""
    outfit: str = ""
    activity: str = ""
    location: str = ""
    mood: str = ""
    selfie_scene: str = ""
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
            activity=str(data.get("activity") or "").strip(),
            location=str(data.get("location") or "").strip(),
            mood=str(data.get("mood") or "").strip(),
            selfie_scene=str(data.get("selfie_scene") or "").strip(),
            selfie_prompt_hint=str(data.get("selfie_prompt_hint") or "").strip(),
            caption_hint=str(data.get("caption_hint") or "").strip(),
        )

    def contains(self, moment: datetime.datetime, *, window_start: datetime.datetime) -> bool:
        start_dt = resolve_clock_in_window(self.start_time, window_start=window_start)
        end_dt = resolve_clock_in_window(self.end_time, window_start=window_start)
        if end_dt <= start_dt:
            end_dt += datetime.timedelta(days=1)
        return start_dt <= moment < end_dt


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


@dataclass(slots=True)
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
        if not self.segments:
            self.segments = build_default_segments(
                anchor_dt=anchor_dt,
                outfit_style=self.outfit_style or "自然日常风",
                summary_outfit=self.summary_outfit or self.outfit or "自然舒服的日常穿搭",
                summary_schedule=self.summary_schedule or self.schedule or "按自己的节奏安排一天",
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
