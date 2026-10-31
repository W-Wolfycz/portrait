const bridge = window.AstrBotPluginPage;

const state = {
  sessions: [],
  selected: "",
  query: "",
};

const $ = (id) => document.getElementById(id);
const TIER_LABELS = { long: "长期", mid: "中期", short: "近期" };
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
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function labelFor(key) {
  return FIELD_LABELS[key] || key;
}

function typeLabel(type) {
  if (type === "GroupMessage") return "群聊";
  if (type === "FriendMessage") return "私聊";
  return type || "会话";
}

function shortTime(value) {
  if (!value) return "";
  const text = String(value).replace("T", " ");
  return text.slice(0, 16);
}

function renderProfile(profile) {
  const wrap = el("div", "profile-grid");
  if (!profile || !Object.keys(profile).length) {
    wrap.append(el("p", "empty", "暂无画像"));
    return wrap;
  }
  const tiers = ["long", "mid", "short"].filter(
    (tier) => profile[tier] && typeof profile[tier] === "object"
  );
  if (!tiers.length) {
    const card = el("div", "card");
    card.append(el("h3", null, "画像"));
    const pre = el("pre", null, JSON.stringify(profile, null, 2));
    card.append(pre);
    wrap.append(card);
    return wrap;
  }
  for (const tier of tiers) {
    const card = el("div", "card");
    card.append(el("h3", null, `${TIER_LABELS[tier]}画像`));
    const sections = profile[tier];
    const keys = Object.keys(sections);
    if (!keys.length) {
      card.append(el("p", "empty", "暂无记录"));
    } else {
      for (const key of keys) {
        const row = el("div", "field");
        row.append(el("div", "field-key", labelFor(key)));
        row.append(el("div", "field-value", String(sections[key] ?? "")));
        card.append(row);
      }
    }
    wrap.append(card);
  }
  return wrap;
}

function renderPerson(person) {
  const card = el("article", "person-card");
  const head = el("div", "person-head");
  head.append(el("strong", null, person.name || "未命名"));
  head.append(el("span", "person-id", person.sender_id || ""));
  card.append(head);
  card.append(renderProfile(person.profile));
  return card;
}

function renderDetail(data) {
  const detail = $("detail");
  detail.replaceChildren();

  const head = el("div", "detail-head");
  const title = el("div");
  title.append(el("h2", null, typeLabel(data.message_type)));
  title.append(
    el(
      "p",
      null,
      `${data.session_id || ""} · ${data.platform || ""}`
    )
  );
  head.append(title);
  head.append(el("span", "badge", data.session || ""));
  detail.append(head);

  const groupSection = el("section");
  groupSection.append(el("h3", "section-title", "群画像"));
  groupSection.append(renderProfile(data.group));
  detail.append(groupSection);

  const persons = Array.isArray(data.persons) ? data.persons : [];
  const personSection = el("section");
  personSection.append(
    el("h3", "section-title", `人物画像（${persons.length}）`)
  );
  if (!persons.length) {
    personSection.append(el("p", "empty", "暂无人物画像"));
  } else {
    const grid = el("div", "person-grid");
    for (const person of persons) {
      grid.append(renderPerson(person));
    }
    personSection.append(grid);
  }
  detail.append(personSection);
}

function filteredSessions() {
  const query = state.query.trim().toLowerCase();
  if (!query) return state.sessions;
  return state.sessions.filter((item) =>
    [item.session_id, item.platform, item.message_type, item.session]
      .join(" ")
      .toLowerCase()
      .includes(query)
  );
}

function renderSessions() {
  const list = $("sessions");
  list.replaceChildren();
  const items = filteredSessions();
  if (!items.length) {
    list.append(el("p", "empty", "暂无画像会话"));
    return;
  }
  for (const item of items) {
    const button = el("button", "session", "");
    button.type = "button";
    if (item.session === state.selected) button.classList.add("active");
    button.addEventListener("click", () => selectSession(item.session));

    const title = el("div", "session-title");
    title.append(el("span", null, typeLabel(item.message_type)));
    title.append(el("span", "badge", `${item.persons?.length || 0} 人`));
    button.append(title);
    button.append(
      el(
        "div",
        "session-meta",
        `${item.session_id || ""} · ${item.platform || ""}` +
          (item.updated_at ? ` · ${shortTime(item.updated_at)}` : "")
      )
    );
    list.append(button);
  }
}

async function selectSession(session) {
  state.selected = session;
  renderSessions();
  const detail = $("detail");
  detail.replaceChildren(el("p", "loading", "加载中…"));
  try {
    const data = await bridge.apiGet("profile", { session });
    renderDetail(data);
  } catch (error) {
    detail.replaceChildren(
      el("div", "notice", `读取画像失败：${error.message || error}`)
    );
  }
}

async function loadSessions() {
  const counts = $("counts");
  counts.textContent = "加载中…";
  try {
    const data = await bridge.apiGet("sessions");
    state.sessions = Array.isArray(data.items) ? data.items : [];
    const c = data.counts || {};
    counts.textContent = `会话 ${c.sessions || 0} · 人物 ${c.persons || 0} · 待合并 ${c.pending_segments || 0}`;
    renderSessions();
    if (!state.sessions.length) {
      $("detail").replaceChildren(
        el("p", "empty", "还没有画像。等待自动整理，或先执行 /portrait merge。")
      );
      return;
    }
    const exists = state.sessions.some((item) => item.session === state.selected);
    if (!exists) {
      await selectSession(state.sessions[0].session);
    } else {
      await selectSession(state.selected);
    }
  } catch (error) {
    counts.textContent = "加载失败";
    $("detail").replaceChildren(
      el("div", "notice", `读取列表失败：${error.message || error}`)
    );
  }
}

$("refresh").addEventListener("click", loadSessions);
$("search").addEventListener("input", (event) => {
  state.query = event.target.value || "";
  renderSessions();
});

await bridge.ready();
await loadSessions();
