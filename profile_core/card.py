"""画像卡片：把画 payload 转成可直接 Playwright 渲染的 HTML。"""

from __future__ import annotations

from typing import Any

FIELD_LABELS = {
    "identity": "身份",
    "role": "角色",
    "style": "表达风格",
    "interests": "兴趣",
    "other": "其他",
    "schedule": "作息",
    "relations": "关系",
    "recent": "最近",
    "theme": "主题",
    "atmosphere": "氛围",
    "norms": "规则/共识",
    "memes": "梗/黑话",
    "active_hours": "活跃时段",
    "members": "成员",
    "recurring": "固定活动",
    "request_pattern": "请求习惯",
    "media_interests": "媒介兴趣",
    "technical_interests": "技术关注",
    "persona_details": "人设细节",
    "relations_p1": "关系（指定成员）",
    "relations_extra": "其他关系",
    "recent_games": "近期游戏",
    "recent_account": "近期账号",
    "recent_misc": "近期杂项",
    "recent_mood": "近期情绪",
    "recent_snow": "近期 SnowLuma",
    "recent_display": "近期展示",
    "speech_habits": "说话习惯",
    "occupational_background": "职业背景",
    "game_profile": "游戏画像",
    "food_habits": "饮食习惯",
    "tech_concerns": "技术关注",
    "ai_tools": "AI 工具",
    "nickname_count": "昵称数量",
    "display_name": "显示名",
    "local_policy": "本地政策",
    "technical_recent": "近期技术",
    "work": "工作",
    "location": "位置",
    "gaming": "游戏",
    "language": "语言",
    "habits": "习惯",
    "tech": "技术",
    "bot_use": "机器人使用",
    "personality": "性格",
    "hobbies": "爱好",
    "education": "教育",
    "occupation": "职业",
    "skills": "技能",
    "family": "家庭",
    "friends": "朋友",
    "travel": "旅行",
    "health": "健康",
    "fitness": "健身",
    "shopping": "购物",
    "money": "消费",
}

FIELD_TOKENS = {
    "request": "请求",
    "pattern": "模式",
    "media": "媒介",
    "technical": "技术",
    "tech": "技术",
    "recent": "近期",
    "games": "游戏",
    "game": "游戏",
    "account": "账号",
    "misc": "杂项",
    "mood": "情绪",
    "snow": "SnowLuma",
    "display": "展示",
    "speech": "说话",
    "habits": "习惯",
    "habit": "习惯",
    "occupational": "职业",
    "background": "背景",
    "profile": "画像",
    "food": "饮食",
    "concerns": "关注",
    "ai": "AI",
    "tools": "工具",
    "nickname": "昵称",
    "count": "数量",
    "local": "本地",
    "policy": "政策",
    "activity": "活动",
    "social": "社交",
    "extra": "附加",
    "detail": "细节",
    "details": "细节",
    "favorite": "偏好",
    "preferences": "偏好",
    "skill": "技能",
    "skills": "技能",
    "status": "状态",
    "info": "信息",
    "note": "备注",
    "notes": "备注",
    "style": "风格",
    "role": "角色",
    "relation": "关系",
    "relations": "关系",
    "schedule": "作息",
    "interests": "兴趣",
    "identity": "身份",
    "members": "成员",
    "member": "成员",
    "active": "活跃",
    "hours": "时段",
    "work": "工作",
    "job": "工作",
    "location": "位置",
    "gaming": "游戏",
    "language": "语言",
    "bot": "机器人",
    "use": "使用",
    "personality": "性格",
    "hobby": "爱好",
    "education": "教育",
    "school": "学校",
    "major": "专业",
    "family": "家庭",
    "friends": "朋友",
    "music": "音乐",
    "movie": "影视",
    "movies": "影视",
    "anime": "二次元",
    "travel": "旅行",
    "health": "健康",
    "fitness": "健身",
    "money": "消费",
    "shopping": "购物",
    "device": "设备",
    "devices": "设备",
    "programming": "编程",
    "code": "代码",
    "software": "软件",
    "hardware": "硬件",
    "study": "学习",
    "sleep": "睡眠",
    "age": "年龄",
    "gender": "性别",
}


def field_label(key: str) -> str:
    text = str(key or "").strip()
    if text in FIELD_LABELS:
        return FIELD_LABELS[text]
    if not text:
        return "自定义"
    translated = "".join(
        FIELD_TOKENS.get(part, part) for part in text.split("_") if part
    )
    return translated if translated != text else "自定义"

PROFILE_CARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px;
    background: #eef3f9;
    color: #1e293b;
    font-family: "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif;
  }
  .card {
    width: 720px;
    margin: 0 auto;
    padding: 28px 30px 32px;
    border-radius: 18px;
    background: #fff;
    box-shadow: 0 8px 28px rgba(15, 23, 42, .10);
  }
  .head {
    display: flex;
    align-items: center;
    gap: 16px;
    padding-bottom: 20px;
    border-bottom: 1px solid #e2e8f0;
  }
  .avatar {
    width: 68px;
    height: 68px;
    border-radius: 50%;
    object-fit: cover;
    background: #e2e8f0;
  }
  h1 { margin: 0; font-size: 26px; line-height: 1.3; }
  .sub { margin: 6px 0 0; color: #64748b; font-size: 13px; }
  .section { margin-top: 20px; }
  .section h2 {
    margin: 0 0 8px;
    color: #2563eb;
    font-size: 15px;
    font-weight: 600;
  }
  .field {
    display: grid;
    grid-template-columns: 190px minmax(0, 1fr);
    gap: 12px;
    padding: 8px 10px;
    border-bottom: 1px solid #f1f5f9;
    font-size: 14px;
    line-height: 1.65;
  }
  .field:last-child { border-bottom: 0; }
  .key { color: #64748b; overflow-wrap: anywhere; }
  .key-en { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
  .key-zh { margin-left: 5px; color: #94a3b8; font-size: 11px; }
  .value { color: #1e293b; white-space: pre-wrap; overflow-wrap: anywhere; }
  .empty { color: #94a3b8; font-size: 13px; }
  .foot { margin-top: 20px; color: #94a3b8; font-size: 11px; }
</style>
</head>
<body>
  <main class="card">
    <header class="head">
      {% if avatar %}<img class="avatar" src="{{ avatar|e }}" alt="">{% endif %}
      <div>
        <h1>{{ title|e }}</h1>
        <p class="sub">{{ subtitle|e }}</p>
      </div>
    </header>
    {% for section in sections %}
    <section class="section">
      <h2>{{ section.name|e }}</h2>
      {% for field in section.fields %}
      <div class="field">
        <div class="key">
          <span class="key-en">{{ field.key|e }}</span>
          <span class="key-zh">（{{ field.zh|e }}）</span>
        </div>
        <div class="value">{{ field.value|e }}</div>
      </div>
      {% endfor %}
    </section>
    {% endfor %}
    {% if not sections %}<p class="empty">暂无画像内容</p>{% endif %}
    <p class="foot">群像 · 画像快照</p>
  </main>
</body>
</html>"""


def profile_sections(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tier, name in (("long", "长期"), ("mid", "中期"), ("short", "近期")):
        sections = profile.get(tier) if isinstance(profile, dict) else None
        if not isinstance(sections, dict):
            continue
        fields = [
            {
                "key": str(key),
                "zh": field_label(str(key)),
                "value": str(value or "").strip(),
            }
            for key, value in sections.items()
            if str(value or "").strip()
        ]
        if fields:
            result.append({"name": name, "fields": fields})
    return result


def card_data(
    *,
    title: str,
    subtitle: str,
    avatar: str,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "title": title,
        "subtitle": subtitle,
        "avatar": avatar,
        "sections": profile_sections(profile),
    }


def card_markdown(data: dict[str, Any]) -> str:
    lines = [
        f"# {data.get('title', '画像')}",
        f"*{data.get('subtitle', '')}*",
        "",
    ]
    for section in data.get("sections") or []:
        lines.append(f"## {section.get('name', '')}")
        for field in section.get("fields") or []:
            lines.append(
                f"- **{field.get('key', '')}（{field.get('zh', '自定义')}）**："
                f"{field.get('value', '')}"
            )
        lines.append("")
    return "\n".join(lines).strip()
