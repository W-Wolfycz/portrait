const bridge = window.AstrBotPluginPage;

const state = {
  sessions: [],
  selected: "",
  data: null,
  tab: "group",
  personQuery: "",
  sessionQuery: "",
  counts: {},
  status: {},
  editing: null,
  person: null,
};

const $ = (id) => document.getElementById(id);
const TIER_LABELS = { long: "长期", mid: "中期", short: "近期" };
const KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;
const FIELD_LABELS = {
  identity: "身份",
  role: "角色",
  style: "表达风格",
  interests: "兴趣",
  other: "其他",
  schedule: "作息",
  relations: "关系",
  recent: "最近",
  theme: "主题",
  atmosphere: "氛围",
  norms: "规则/共识",
  memes: "梗/黑话",
  active_hours: "活跃时段",
  members: "成员",
  recurring: "固定活动",
  request_pattern: "请求习惯",
  media_interests: "媒介兴趣",
  technical_interests: "技术关注",
  persona_details: "人设细节",
  relations_p1: "关系（指定成员）",
  relations_extra: "其他关系",
  recent_games: "近期游戏",
  recent_account: "近期账号",
  recent_misc: "近期杂项",
  recent_mood: "近期情绪",
  recent_snow: "近期 SnowLuma",
  recent_display: "近期展示",
  speech_habits: "说话习惯",
  occupational_background: "职业背景",
  game_profile: "游戏画像",
  food_habits: "饮食习惯",
  tech_concerns: "技术关注",
  ai_tools: "AI 工具",
  nickname_count: "昵称数量",
  display_name: "显示名",
  local_policy: "本地政策",
  technical_recent: "近期技术",
  work: "工作",
  location: "位置",
  gaming: "游戏",
  language: "语言",
  habits: "习惯",
  tech: "技术",
  bot_use: "机器人使用",
  personality: "性格",
  hobbies: "爱好",
  education: "教育",
  occupation: "职业",
  skills: "技能",
  family: "家庭",
  friends: "朋友",
  travel: "旅行",
  health: "健康",
  fitness: "健身",
  shopping: "购物",
  money: "消费",
};

const COMMON_FIELDS = {
  person: {
    long: ["identity", "role", "style", "interests", "other"],
    mid: ["schedule", "relations"],
    short: ["recent"],
  },
  group: {
    long: ["theme", "atmosphere", "norms", "memes"],
    mid: ["active_hours", "members", "relations", "recurring"],
    short: ["recent"],
  },
};

const FIELD_TOKENS = {
  request: "请求",
  pattern: "模式",
  media: "媒介",
  technical: "技术",
  tech: "技术",
  recent: "近期",
  games: "游戏",
  game: "游戏",
  account: "账号",
  misc: "杂项",
  mood: "情绪",
  snow: "SnowLuma",
  display: "展示",
  speech: "说话",
  habits: "习惯",
  habit: "习惯",
  occupational: "职业",
  background: "背景",
  profile: "画像",
  food: "饮食",
  concerns: "关注",
  ai: "AI",
  tools: "工具",
  nickname: "昵称",
  count: "数量",
  local: "本地",
  policy: "政策",
  activity: "活动",
  social: "社交",
  group: "群",
  person: "人物",
  extra: "附加",
  detail: "细节",
  details: "细节",
  favorite: "偏好",
  preferences: "偏好",
  skill: "技能",
  skills: "技能",
  status: "状态",
  info: "信息",
  note: "备注",
  notes: "备注",
  style: "风格",
  role: "角色",
  relation: "关系",
  relations: "关系",
  schedule: "作息",
  interests: "兴趣",
  identity: "身份",
  members: "成员",
  member: "成员",
  activity_hours: "活跃时段",
  active: "活跃",
  hours: "时段",
  work: "工作",
  job: "工作",
  location: "位置",
  gaming: "游戏",
  language: "语言",
  bot: "机器人",
  use: "使用",
  personality: "性格",
  hobby: "爱好",
  hobbies: "爱好",
  education: "教育",
  school: "学校",
  major: "专业",
  family: "家庭",
  friends: "朋友",
  music: "音乐",
  movie: "影视",
  movies: "影视",
  anime: "二次元",
  travel: "旅行",
  health: "健康",
  fitness: "健身",
  money: "消费",
  shopping: "购物",
  device: "设备",
  devices: "设备",
  programming: "编程",
  code: "代码",
  software: "软件",
  hardware: "硬件",
  study: "学习",
  sleep: "睡眠",
  age: "年龄",
  gender: "性别",
};

const FIELD_ZH_TO_KEY = Object.fromEntries(
  Object.entries(FIELD_LABELS)
    .flatMap(([key, label]) => [
      [label, key],
      [label.replace(/[（）()]/g, ""), key],
    ])
    .reverse()
);

function normalizeFieldKey(raw) {
  const text = String(raw || "").trim();
  if (KEY_RE.test(text)) return text;
  if (FIELD_ZH_TO_KEY[text]) return FIELD_ZH_TO_KEY[text];
  const lowered = text.toLowerCase().replace(/[\s-]+/g, "_");
  return KEY_RE.test(lowered) ? lowered : "";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function typeLabel(type) {
  if (type === "GroupMessage") return "群聊";
  if (type === "FriendMessage") return "私聊";
  return type || "会话";
}

function shortTime(value) {
  if (!value) return "";
  return String(value).replace("T", " ").slice(0, 16);
}

function displayName(person) {
  const name = String(person.name || "").trim();
  const sender = String(person.sender_id || "").trim();
  if (!name) return sender ? `QQ ${sender}` : "未命名";
  if (/^\d+$/.test(name)) return `QQ ${name}`;
  return name;
}

function initialOf(name) {
  const text = String(name || "").trim();
  return text ? Array.from(text)[0] : "?";
}

function avatarNode(url, name, className = "") {
  const wrap = el("span", `avatar ${className}`.trim());
  const fallback = () => {
    wrap.replaceChildren(el("span", "avatar-text", initialOf(name)));
  };
  if (!url) {
    fallback();
    return wrap;
  }
  const img = document.createElement("img");
  img.src = url;
  img.alt = "";
  img.loading = "lazy";
  img.referrerPolicy = "no-referrer";
  img.addEventListener("error", fallback, { once: true });
  wrap.append(img);
  return wrap;
}

function fieldLabel(key) {
  if (FIELD_LABELS[key]) return FIELD_LABELS[key];
  const text = String(key || "").trim();
  if (!text) return "自定义";
  return text
    .split("_")
    .filter(Boolean)
    .map((part) => FIELD_TOKENS[part] || part)
    .join("");
}

function tiersOf(profile) {
  if (!profile || typeof profile !== "object") return [];
  return ["long", "mid", "short"].filter(
    (tier) => profile[tier] && typeof profile[tier] === "object" &&
      Object.keys(profile[tier]).length
  );
}

function tierCoverage(profile) {
  const tiers = tiersOf(profile);
  const wrap = el("span", "tier-dots");
  wrap.title = `已记录：${tiers.map((t) => TIER_LABELS[t]).join(" / ") || "无"}`;
  for (const tier of ["long", "mid", "short"]) {
    wrap.append(el("span", tiers.includes(tier) ? "tier-dot on" : "tier-dot"));
  }
  return wrap;
}

function previewOf(profile) {
  if (!profile || typeof profile !== "object") return "暂无画像";
  const long = profile.long || {};
  const short = profile.short || {};
  return (
    long.identity ||
    long.role ||
    long.style ||
    short.recent ||
    long.interests ||
    "暂无画像"
  );
}

function renderProfile(profile) {
  const wrap = el("div", "profile-grid");
  if (!profile || !Object.keys(profile).length) {
    wrap.append(el("p", "empty", "暂无画像"));
    return wrap;
  }
  const tiers = tiersOf(profile);
  if (!tiers.length) {
    const card = el("div", "card");
    card.append(el("h3", null, "画像"));
    card.append(el("pre", null, JSON.stringify(profile, null, 2)));
    wrap.append(card);
    return wrap;
  }
  for (const tier of tiers) {
    const card = el("div", "card");
    card.append(el("h3", null, `${TIER_LABELS[tier]}画像`));
    const sections = profile[tier];
    for (const key of Object.keys(sections)) {
      const row = el("div", "field");
      const keyNode = el("div", "field-key");
      keyNode.title = key;
      keyNode.append(el("span", "field-key-code", key));
      keyNode.append(
        el("span", "field-key-zh", `（${fieldLabel(key)}）`)
      );
      if (!/[\u4e00-\u9fff]/.test(fieldLabel(key))) {
        keyNode.append(el("span", "field-custom", "自定义"));
      }
      row.append(keyNode);
      row.append(el("div", "field-value", String(sections[key] ?? "")));
      card.append(row);
    }
    wrap.append(card);
  }
  return wrap;
}

function pendingNotice(data) {
  const pending = Number(data?.pending || 0);
  const unextracted = Number(data?.unextracted || 0);
  if (!pending && !unextracted) return null;
  const parts = [];
  if (unextracted) parts.push(`${unextracted} 条消息待萃取`);
  if (pending) parts.push(`${pending} 个片段待合并`);
  return el(
    "div",
    "notice",
    `本会话还有 ${parts.join("，")}。执行 /portrait scan 或 merge 会继续处理。`
  );
}

function renderGroup() {
  const data = state.data;
  const section = el("section");
  section.append(renderProfile(data.group));
  return section;
}

function personMatches(person, query) {
  if (!query) return true;
  const text = `${displayName(person)} ${person.sender_id || ""} ${person.name || ""}`;
  return text.toLowerCase().includes(query.toLowerCase());
}

function renderPersons() {
  const data = state.data;
  const persons = Array.isArray(data.persons) ? data.persons : [];
  const section = el("section");
  const toolbar = el("div", "person-toolbar");
  const search = el("input", "search");
  search.type = "search";
  search.placeholder = "搜人物名 / QQ 号";
  search.value = state.personQuery;
  const count = el("span", "muted");
  const grid = el("div", "person-grid");
  section.append(toolbar, grid);
  toolbar.append(search, count);

  const paint = () => {
    const filtered = persons.filter((person) =>
      personMatches(person, state.personQuery)
    );
    count.textContent = state.personQuery
      ? `${filtered.length} / ${persons.length} 人`
      : `${persons.length} 人`;
    grid.replaceChildren();

    if (!filtered.length) {
      grid.append(
        el("p", "empty", persons.length ? "没有匹配的人物。" : "暂无人物画像。")
      );
      return;
    }

    for (const person of filtered) {
      const profile = person.profile || {};
      const card = el("button", "person-card");
      card.type = "button";
      card.addEventListener("click", () => openPerson(person));

      const head = el("div", "person-head");
      const personMeta = el("div", "person-meta");
      personMeta.append(el("span", "person-name", displayName(person)));
      personMeta.append(el("span", "person-id", person.sender_id || ""));
      head.append(
        avatarNode(person.avatar, displayName(person), "avatar-small")
      );
      head.append(personMeta);
      card.append(head);
      card.append(el("div", "person-preview", previewOf(profile)));

      const foot = el("div", "person-foot");
      foot.append(tierCoverage(profile));
      const tiers = tiersOf(profile).length;
      if (tiers < 3) {
        foot.append(el("span", "badge warn", tiers ? "信息较少" : "无画像"));
      } else {
        foot.append(el("span", "badge", "三层完整"));
      }
      card.append(foot);
      grid.append(card);
    }
  };

  search.addEventListener("input", (event) => {
    state.personQuery = event.target.value || "";
    paint();
  });
  paint();
  return section;
}

function renderBody() {
  const body = $("detail-body");
  body.replaceChildren();
  if (!state.data) return;
  if (state.tab === "group") {
    body.append(renderGroup());
  } else {
    body.append(renderPersons());
  }
}

function setTab(tab) {
  state.tab = tab;
  renderDetail(state.data);
}

function renderDetail(data) {
  const detail = $("detail");
  detail.replaceChildren();
  if (!data) return;

  const isGroup = data.message_type === "GroupMessage";
  const groupHasContent = data.group && Object.keys(data.group).length > 0;
  if (isGroup && state.tab === "group" && !groupHasContent) {
    state.tab = "persons";
  } else if (!isGroup) {
    state.tab = "persons";
  } else if (!state.tab) {
    state.tab = "group";
  }

  const head = el("div", "detail-head");
  const identity = el("div", "detail-identity");
  identity.append(
    avatarNode(
      data.avatar,
      data.display_name || typeLabel(data.message_type),
      "avatar-large"
    )
  );
  const title = el("div");
  title.append(
    el("h2", null, data.display_name || typeLabel(data.message_type))
  );
  title.append(
    el(
      "p",
      "detail-sub",
      `${typeLabel(data.message_type)} · ${data.session_id || ""} · ` +
        `${data.platform || ""}`
    )
  );
  identity.append(title);
  head.append(identity);

  const badges = el("div", "head-badges");
  badges.append(el("span", "badge blue", `${(data.persons || []).length} 人`));
  if (Number(data.unextracted || 0) > 0) {
    badges.append(
      el("span", "badge", `待萃取 ${Number(data.unextracted)}`)
    );
  }
  if (Number(data.pending || 0) > 0) {
    badges.append(
      el("span", "badge warn", `待合并 ${Number(data.pending)}`)
    );
  }
  if (isGroup) {
    const edit = el("button", "button", "编辑群画像");
    edit.type = "button";
    edit.addEventListener("click", () => openEditor("group"));
    badges.append(edit);
  }
  head.append(badges);
  detail.append(head);

  const notice = pendingNotice(data);
  if (notice) detail.append(notice);
  detail.append(
    el(
      "div",
      "notice info",
      "画像会随每日合并持续更新；你的手动修改会成为下一次合并的旧画像，不是永久锁定。"
    )
  );

  const tabs = el("div", "tabs");
  if (isGroup && groupHasContent) {
    const groupTab = el("button", "tab", "群画像");
    groupTab.type = "button";
    groupTab.classList.toggle("active", state.tab === "group");
    groupTab.addEventListener("click", () => setTab("group"));
    tabs.append(groupTab);
  }
  const personTab = el("button", "tab", `人物 ${(data.persons || []).length}`);
  personTab.type = "button";
  personTab.classList.toggle("active", state.tab === "persons");
  personTab.addEventListener("click", () => setTab("persons"));
  tabs.append(personTab);
  detail.append(tabs);

  const body = el("div");
  body.id = "detail-body";
  detail.append(body);
  renderBody();
}

function cloneProfile(profile) {
  return JSON.parse(JSON.stringify(profile || {}));
}

function makeEditorRow(tier, key = "", value = "", kind = "person", onRemove = null) {
  const row = el("div", "editor-row");
  const keyWrap = el("div", "editor-key-wrap");
  const common = COMMON_FIELDS[kind][tier];
  const select = document.createElement("select");
  select.className = "editor-key-select";
  const customOption = document.createElement("option");
  customOption.value = "";
  customOption.textContent = "自定义字段 / 输入 key";
  select.append(customOption);
  for (const item of common) {
    const option = document.createElement("option");
    option.value = item;
    option.textContent = `${item}（${fieldLabel(item)}）`;
    select.append(option);
  }
  select.value = common.includes(key) ? key : "";

  const keyInput = document.createElement("input");
  keyInput.className = "editor-key";
  keyInput.placeholder = "field_name 或 常用中文名";
  keyInput.value = key;
  const hint = el("div", "editor-key-hint");
  const updateHint = () => {
    const current = keyInput.value.trim();
    const normalized = normalizeFieldKey(current);
    if (!current) {
      hint.textContent = "可从上方选择常用字段，或直接输入 key / 中文名";
    } else if (!normalized) {
      hint.textContent = "无法识别：请用常用中文名或 snake_case 英文 key";
    } else if (FIELD_LABELS[normalized]) {
      hint.textContent =
        current === normalized
          ? `${normalized}（${FIELD_LABELS[normalized]}）`
          : `${current} → ${normalized}（${FIELD_LABELS[normalized]}）`;
    } else if (fieldLabel(normalized) !== normalized) {
      hint.textContent = `${normalized}（${fieldLabel(normalized)}·自定义）`;
    } else {
      hint.textContent = `自定义字段 · ${normalized}`;
    }
  };
  select.addEventListener("change", () => {
    if (select.value) {
      keyInput.value = select.value;
      updateHint();
    } else {
      keyInput.focus();
    }
  });
  keyInput.addEventListener("input", () => {
    const current = keyInput.value.trim();
    select.value = common.includes(current) ? current : "";
    updateHint();
  });
  updateHint();
  keyWrap.append(select, keyInput, hint);
  const valueInput = document.createElement("textarea");
  valueInput.className = "editor-value";
  valueInput.placeholder = "画像内容";
  valueInput.value = value;
  const remove = el("button", "editor-remove", "×");
  remove.type = "button";
  remove.title = "删除这个字段";
  remove.addEventListener("click", () => {
    row.remove();
    if (onRemove) onRemove();
  });
  row.append(keyWrap, valueInput, remove);
  row.dataset.tier = tier;
  return row;
}

function openEditor(kind, person = null) {
  const data = state.data;
  if (!data) return;
  if (kind === "group" && data.message_type !== "GroupMessage") return;
  const source =
    kind === "group" ? data.group || {} : person?.profile || {};
  const profile = cloneProfile(source);
  for (const tier of ["long", "mid", "short"]) {
    if (!profile[tier] || typeof profile[tier] !== "object") {
      profile[tier] = {};
    }
  }

  state.editing = {
    kind,
    person,
    session: state.selected,
    profile,
  };

  const title =
    kind === "group"
      ? `编辑群画像 · ${data.display_name || data.session_id}`
      : `编辑人物画像 · ${displayName(person)}`;
  $("editor-title").textContent = title;
  $("editor-sub").textContent =
    kind === "person" ? person.sender_id || "" : "群画像";
  $("editor-hint").textContent =
    "字段名使用 snake_case；常用字段可直接点选，自定义字段直接输入。空字段不会保存。";

  const form = $("editor-form");
  form.replaceChildren();

  if (kind === "person") {
    const wrap = el("div", "editor-name-wrap");
    wrap.append(el("label", "editor-name-label", "显示名"));
    const nameInput = document.createElement("input");
    nameInput.className = "editor-name";
    nameInput.value = person?.name || "";
    nameInput.placeholder = "留空保留当前显示名";
    wrap.append(nameInput);
    form.append(wrap);
  }

  for (const tier of ["long", "mid", "short"]) {
    const section = el("section", "editor-tier");
    section.dataset.tier = tier;
    const head = el("div", "editor-tier-head");
    head.append(el("h3", null, `${TIER_LABELS[tier]}画像`));
    const add = el("button", "button", "新增字段");
    add.type = "button";
    add.addEventListener("click", () => {
      const empty = section.querySelector(".editor-empty");
      if (empty) empty.remove();
      section.append(makeEditorRow(tier, "", "", kind));
    });
    head.append(add);
    section.append(head);

    const keys = Object.keys(profile[tier] || {});
    const existing = new Set(keys);
    const suggestions = el("div", "editor-suggestions");
    for (const key of COMMON_FIELDS[kind][tier]) {
      if (existing.has(key)) continue;
      const chip = el(
        "button",
        "editor-suggest",
        `${key}（${fieldLabel(key)}）`
      );
      chip.type = "button";
      chip.addEventListener("click", () => {
        const empty = section.querySelector(".editor-empty");
        if (empty) empty.remove();
        const row = makeEditorRow(tier, key, "", kind, () => {
          if (!suggestions.contains(chip)) suggestions.append(chip);
        });
        section.append(row);
        chip.remove();
      });
      suggestions.append(chip);
    }
    if (suggestions.childElementCount) section.append(suggestions);

    if (!keys.length) {
      section.append(el("p", "editor-empty", "暂无字段，点上方常用字段或“新增字段”。"));
    }
    for (const key of keys) {
      section.append(makeEditorRow(tier, key, profile[tier][key], kind));
    }
    form.append(section);
  }

  $("editor").hidden = false;
  $("editor-form").scrollTop = 0;
}

function closeEditor() {
  state.editing = null;
  $("editor").hidden = true;
}

function collectEditor() {
  if (!state.editing) return null;
  const form = $("editor-form");
  const profile = { long: {}, mid: {}, short: {} };
  for (const tier of ["long", "mid", "short"]) {
    const section = form.querySelector(`.editor-tier[data-tier="${tier}"]`);
    if (!section) continue;
    for (const row of section.querySelectorAll(".editor-row")) {
      const rawKey = row.querySelector(".editor-key")?.value.trim() || "";
      const key = normalizeFieldKey(rawKey);
      const value = row.querySelector(".editor-value")?.value.trim() || "";
      if (!rawKey || !value) continue;
      if (!key) {
        $("editor-hint").textContent =
          `字段名“${rawKey}”无法识别：请用常用中文名或 snake_case。`;
        return null;
      }
      profile[tier][key] = value;
    }
  }
  for (const tier of ["long", "mid", "short"]) {
    if (!Object.keys(profile[tier]).length) delete profile[tier];
  }
  const nameInput = form.querySelector(".editor-name");
  return {
    session: state.editing.session,
    kind: state.editing.kind,
    sender_id: state.editing.person?.sender_id || "",
    display_name: nameInput?.value.trim() || "",
    profile,
  };
}

async function saveEditor() {
  const payload = collectEditor();
  if (!payload) return;
  if (!Object.keys(payload.profile).length) {
    $("editor-hint").textContent = "至少保留一个非空字段。";
    return;
  }
  $("editor-save").disabled = true;
  $("editor-hint").textContent = "保存中…";
  try {
    await bridge.apiPost("update-profile", payload);
    closeEditor();
    await selectSession(state.selected);
  } catch (error) {
    $("editor-hint").textContent = `保存失败：${error.message || error}`;
  } finally {
    $("editor-save").disabled = false;
  }
}

function openPerson(person) {
  state.person = person;
  const drawer = $("drawer");
  $("drawer-title").textContent = displayName(person);
  $("drawer-sub").textContent = person.sender_id || "";
  const body = $("drawer-body");
  body.replaceChildren(renderProfile(person.profile || {}));
  drawer.hidden = false;
  $("drawer-close").focus();
}

function closeDrawer() {
  $("drawer").hidden = true;
}

function renderSessions() {
  const list = $("sessions");
  list.replaceChildren();
  const query = state.sessionQuery.trim().toLowerCase();
  const items = state.sessions.filter((item) => {
    if (!query) return true;
    return [item.session_id, item.platform, item.message_type, item.session]
      .join(" ")
      .toLowerCase()
      .includes(query);
  });
  $("session-count").textContent = `${items.length} / ${state.sessions.length}`;
  if (!items.length) {
    list.append(el("p", "empty", "暂无画像会话"));
    return;
  }
  for (const item of items) {
    const button = el("button", "session");
    button.type = "button";
    if (item.session === state.selected) button.classList.add("active");
    button.addEventListener("click", () => selectSession(item.session));

    const row = el("div", "session-row");
    const content = el("div", "session-main");
    const title = el("div", "session-title");
    title.append(
      el(
        "span",
        null,
        item.display_name || typeLabel(item.message_type)
      )
    );
    title.append(el("span", "badge", `${item.persons?.length || 0} 人`));
    if (Number(item.unextracted || 0) > 0) {
      title.append(
        el("span", "badge", `待萃取 ${Number(item.unextracted)}`)
      );
    }
    if (Number(item.pending || 0) > 0) {
      title.append(
        el("span", "badge warn", `待合并 ${Number(item.pending)}`)
      );
    }
    content.append(title);
    content.append(
      el(
        "div",
        "session-meta",
        `${item.session_id || ""} · ${item.platform || ""}` +
          (item.updated_at ? ` · ${shortTime(item.updated_at)}` : "")
      )
    );
    row.append(
      avatarNode(
        item.avatar,
        item.display_name || typeLabel(item.message_type),
        "avatar-small"
      )
    );
    row.append(content);
    button.append(row);
    list.append(button);
  }
}

function renderStatus(data) {
  const status = data.status || {};
  const counts = data.counts || {};
  state.status = status;
  state.counts = counts;
  const ready = Boolean(data.ready || status.ready);
  const chip = $("status");
  chip.textContent = ready ? "运行中" : "未就绪";
  chip.classList.toggle("offline", !ready);

  const parts = [
    `会话 ${counts.sessions || 0}`,
    `人物 ${counts.persons || 0}`,
  ];
  const pendingTotal = state.sessions.reduce(
    (sum, item) => sum + Number(item.pending || 0),
    0
  );
  if (pendingTotal) parts.push(`待合并总计 ${pendingTotal}`);
  if (status.interval) parts.push(`间隔 ${status.interval}s`);
  if (status.last_merge) parts.push(`最近合并 ${status.last_merge}`);
  $("stats").textContent = parts.join(" · ");
}

async function selectSession(session) {
  state.selected = session;
  state.tab = "group";
  state.personQuery = "";
  renderSessions();
  const detail = $("detail");
  detail.replaceChildren(el("p", "empty", "加载中…"));
  try {
    const data = await bridge.apiGet("profile", { session });
    state.data = data;
    if (data.message_type !== "GroupMessage") state.tab = "persons";
    renderDetail(data);
  } catch (error) {
    detail.replaceChildren(
      el("div", "notice", `读取画像失败：${error.message || error}`)
    );
  }
}

async function loadSessions() {
  const chip = $("status");
  chip.textContent = "加载中…";
  $("stats").textContent = "";
  try {
    const data = await bridge.apiGet("sessions");
    state.sessions = Array.isArray(data.items) ? data.items : [];
    renderStatus(data);
    renderSessions();
    if (!state.sessions.length) {
      $("detail").replaceChildren(
        el("p", "empty", "还没有画像。等待自动整理，或先执行 /portrait merge。")
      );
      return;
    }
    const exists = state.sessions.some((item) => item.session === state.selected);
    const next = exists ? state.selected : state.sessions[0].session;
    if (next) await selectSession(next);
  } catch (error) {
    chip.textContent = "加载失败";
    chip.classList.add("offline");
    $("detail").replaceChildren(
      el("div", "notice", `读取列表失败：${error.message || error}`)
    );
  }
}

$("refresh").addEventListener("click", loadSessions);
$("search").addEventListener("input", (event) => {
  state.sessionQuery = event.target.value || "";
  renderSessions();
});
$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-edit").addEventListener("click", () => {
  if (state.person) openEditor("person", state.person);
});
$("drawer").addEventListener("click", (event) => {
  if (event.target.dataset.close === "drawer") closeDrawer();
});
$("editor-close").addEventListener("click", closeEditor);
$("editor-cancel").addEventListener("click", closeEditor);
$("editor-save").addEventListener("click", saveEditor);
$("editor").addEventListener("click", (event) => {
  if (event.target.dataset.close === "editor") closeEditor();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("editor").hidden) {
    closeEditor();
  } else if (!$("drawer").hidden) {
    closeDrawer();
  }
});

await bridge.ready();
await loadSessions();
