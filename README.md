# AstrBot Life Scheduler Enhanced Plugin

面向 AstrBot 的拟人化生活日程增强版插件。

它会结合日期、节日、历史日程、近期对话和创意池，自动生成 Bot 当天的穿搭与日程安排，并把这些状态注入到 System Prompt，让 Bot 的日常表现更连续、更像真人。

## 增强内容

- 扩充创意池为更贴近日常的真实内容
- 优化穿搭风格与日程类型的可用性
- 降低空泛、悬浮、难落地词条对生成结果的干扰
- 保留原有自动生成、查看、重写、注入能力

## 功能概览

- 每日自动生成穿搭与日程
- 首次对话懒加载补生成
- 将今日状态注入 System Prompt
- 支持管理员手动重写当日日程
- 支持带补充要求的重写
- 支持调整自动生成时间

## 安装依赖

```bash
pip install holidays APScheduler
```

## 指令

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `查看日程` | 所有人 | 查看今日穿搭和日程 |
| `重写日程` | 管理员 | 重新生成今日日程 |
| `重写日程 <补充要求>` | 管理员 | 带附加要求重写今日日程 |
| `日程时间 <HH:MM>` | 管理员 | 修改每日自动生成时间 |

别名：

- `life show`
- `life renew`
- `life time`

## 配置项

| 字段 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `schedule_time` | string | `07:00` | 每日自动生成日程的时间 |
| `reference_history_days` | int | `3` | 生成时参考的历史日程天数 |
| `reference_recent_count` | int | `10` | 生成时参考的近期会话条数，设为 `0` 表示不参考 |
| `pool` | object | - | 创意池，每次生成随机抽取 |
| `prompt_template` | text | - | LLM 生成日程的 Prompt 模板 |

## 创意池

当前包含 4 组创意池：

- `daily_themes`
- `mood_colors`
- `outfit_styles`
- `schedule_types`

每组均已扩充为 50 条，更偏真实生活、日常场景、稳定可用的生成约束。

## Prompt 占位符

插件支持以下常用占位符：

- `{date_str}`
- `{weekday}`
- `{holiday}`
- `{persona_desc}`
- `{daily_theme}`
- `{mood_color}`
- `{outfit_style}`
- `{schedule_type}`
- `{history_schedules}`
- `{recent_chats}`

## 注入示例

```xml
<character_state>
时间: 下午
穿着: 风格：极简通勤风 ...
日程: 上午处理事务，下午外出办事，晚上回家整理和休息。
</character_state>
```

## 说明

- 本仓库为增强整理版，重点放在“更真实的创意池”和“更稳定的生活感生成”。
- 如果你还在继续扩池，建议优先保持真实、具体、可执行，避免加入太抽象或无实际作用的词条。
