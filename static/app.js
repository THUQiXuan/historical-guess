"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const LAST_GAME_KEY = "wengu:last-game";
  const TIMER_KEY = "wengu:timer-hidden";
  const statuses = { active: "推理中", won: "已猜中", lost: "猜测用尽", abandoned: "已揭晓" };
  const state = { game: null, busy: false, meta: null, view: "play", pending: null, timerHidden: false, libraryOffset: 0, libraryTotal: 0, libraryLoading: false, libraryRevision: 0, historyOffset: 0, historyTotal: 0, historyLoading: false };
  const chatState = { key: "scope", sessions: new Map() };
  let searchTimer;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
  }
  function setHidden(id, value) { $(id).classList.toggle("hidden", value); }
  function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
  function storageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* Private browsing may disable storage. */ } }
  function requestId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function notice(id, message, error = false) {
    const element = $(id);
    element.textContent = message || "";
    element.classList.toggle("hidden", !message);
    element.classList.toggle("error", error);
  }
  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...options.headers } });
    } catch {
      throw new Error("连接中断了。请检查服务是否运行，再次提交即可重试；同一次提交不会重复计数。");
    }
    let data;
    try { data = await response.json(); } catch { throw new Error("服务暂时没有返回有效结果，请稍后重试。"); }
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "提交未成功，请检查输入后重试。";
      throw new Error(detail);
    }
    return data;
  }
  function post(path, body) { return api(path, { method: "POST", body: JSON.stringify(body) }); }
  function presetName(id) { return state.meta?.presets?.find((p) => p.id === id)?.name || (id === "sanguozhi" ? "《三国志》人物" : "广义三国"); }
  function countLabel(count, limit) { return `${count || 0} / ${limit || "不限"}`; }
  function readableDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }
  function yearLabel(person) {
    if (person.birth_year || person.death_year) return `${person.birth_year ?? "?"}—${person.death_year ?? "?"}`;
    return person.active_year ? `${person.active_year} 年见载` : "生卒年未详";
  }
  function updateTimer() {
    if (!state.game) return;
    const seconds = Math.max(0, Math.floor(((state.game.ended_at ? new Date(state.game.ended_at).getTime() : Date.now()) - new Date(state.game.created_at).getTime()) / 1000));
    const safe = Number.isFinite(seconds) ? seconds : 0;
    const hours = Math.floor(safe / 3600);
    const minutes = String(Math.floor(safe / 60) % 60).padStart(2, "0");
    $("game-timer").textContent = `${hours ? `${String(hours).padStart(2, "0")}:` : ""}${minutes}:${String(safe % 60).padStart(2, "0")}`;
    setHidden("timer-wrap", state.timerHidden);
    $("toggle-timer").textContent = state.timerHidden ? "显示计时" : "隐藏计时";
  }
  function sourceLinks(person) {
    const sources = [...(Array.isArray(person.sources) ? person.sources : [])];
    if (person.sanguozhi_evidence?.url && !sources.some((source) => source.url === person.sanguozhi_evidence.url)) sources.push(person.sanguozhi_evidence);
    const links = node("div", "source-links");
    sources.forEach((source) => {
      const value = typeof source === "string" ? source : source.url;
      try {
        const url = new URL(value);
        if (!["https:", "http:"].includes(url.protocol)) return;
        const link = node("a", "", typeof source === "string" ? "查阅来源 ↗" : `${source.title || "查阅来源"} ↗`);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        links.append(link);
      } catch { /* Malformed sources should never create navigable links. */ }
    });
    return links;
  }
  function refreshDisabled() {
    const game = state.game;
    const active = game?.status === "active";
    const questionsDone = active && game.question_limit && game.question_count >= game.question_limit;
    $("start-button").disabled = state.busy || state.meta?.agent?.ready === false;
    $("question-input").disabled = state.busy || !active || Boolean(questionsDone);
    $("question-button").disabled = $("question-input").disabled;
    $("guess-input").disabled = state.busy || !active;
    $("guess-button").disabled = state.busy || !active;
    $("give-up-button").disabled = state.busy || !active;
    $("give-up-yes").disabled = state.busy || !active;
    $("question-hint").textContent = questionsDone ? "提问次数已用尽。你仍可在下方猜人物。" : "每次一个问题，让线索慢慢清晰。";
  }
  function setBusy(busy, message = "裁判正在翻阅史料…") {
    state.busy = busy;
    $("start-button").firstElementChild.textContent = busy && !state.game ? "正在请古人入局…" : "请古人入局";
    $("thinking-text").textContent = message;
    setHidden("thinking", !busy || !state.game);
    refreshDisabled();
  }
  function showView(view, scroll = false) {
    state.view = ["play", "library", "history"].includes(view) ? view : "play";
    document.querySelectorAll(".view").forEach((section) => section.classList.toggle("hidden", section.id !== `view-${state.view}`));
    document.querySelectorAll(".nav-button").forEach((button) => {
      button.classList.toggle("selected", button.dataset.view === state.view);
      if (button.dataset.view === state.view) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (location.hash !== `#${state.view}`) history.replaceState(null, "", `#${state.view}`);
    if (state.view === "library" && !$("character-grid").children.length) loadLibrary(true);
    if (state.view === "history") loadHistory(true);
    if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function renderGame(game, options = {}) {
    const oldId = state.game?.id;
    state.game = game;
    storageSet(LAST_GAME_KEY, game.id);
    if (oldId !== game.id) {
      state.timerHidden = game.timer_enabled === false || storageGet(`${TIMER_KEY}:${game.id}`) === "1";
      $("question-input").value = "";
      $("guess-input").value = "";
      state.pending = null;
    }
    setHidden("game-empty", true);
    setHidden("active-game", false);
    setHidden("give-up-confirm", true);
    const active = game.status === "active";
    $("game-state").className = `status-pill ${game.status}`;
    $("game-state").textContent = statuses[game.status] || game.status;
    $("game-title").textContent = active ? "循迹识人" : "一局留痕";
    const scopeText = game.scope_description || [presetName(game.preset), game.scope].filter(Boolean).join(" · ");
    $("game-scope").textContent = `${scopeText}${game.candidate_count ? ` · ${game.candidate_count} 位候选人物` : ""}`;
    $("question-count").textContent = countLabel(game.question_count, game.question_limit);
    $("guess-count").textContent = countLabel(game.guess_count, game.guess_limit);
    setHidden("game-forms", !active);
    setHidden("give-up-button", !active);
    $("game-footer-note").textContent = active ? "答案在结束时揭晓" : "本局已保存，可在「往局」中回看";
    const conversation = $("conversation");
    conversation.replaceChildren();
    if (!game.events?.length) conversation.append(node("p", "conversation-empty", active ? "人物已入局。第一问，从哪里开始？" : "这一局，还没有留下问答。"));
    (game.events || []).forEach((event, index) => {
      const item = node("article", `event event-${event.kind}`);
      const meta = node("div", "event-meta");
      meta.append(node("span", "event-kind", event.kind === "guess" ? "猜人物" : "问史"), node("span", "", String(index + 1).padStart(2, "0")));
      const content = node("div", "event-question");
      const answerClass = event.answer === "正确" ? "correct" : event.answer === "否" || event.answer === "错误" ? "no" : event.answer === "无法回答" ? "invalid" : "";
      const answerBadge = node(event.withdrawn ? "del" : "span", `event-answer ${answerClass}`, event.answer);
      content.append(node("p", "", event.text), answerBadge);
      item.append(meta, content);
      if (event.corrected) {
        item.append(node("p", "field-hint", `${event.withdrawn ? "史实复核：已撤回，不计次数" : `史实复核：已更正（原答${event.original_answer}）`}。${event.review_note || ""}`));
      }
      if (!active) {
        const explain = node("button", "text-button event-explain", "解释这题 ↗");
        explain.type = "button";
        explain.title = "将这条记录填入复盘聊天，点击发送后解释";
        explain.addEventListener("click", () => {
          selectChat("game");
          fillChatDraft(`请解释本局第 ${index + 1} 条${event.kind === "guess" ? "猜测" : "提问"}记录：\n${event.text}\n记录中的回答是「${event.answer}」。请结合史实解释判断依据；若原判断有误，请明确指出。`);
          $("chat-panel").scrollIntoView({ behavior: "smooth", block: "start" });
          chatSession().notice = "已把这条记录填入输入框，点击「发送」即可复盘。";
          renderChat();
          $("chat-input").focus({ preventScroll: true });
        });
        meta.append(explain);
      }
      conversation.append(item);
    });
    const reveal = $("answer-reveal");
    reveal.replaceChildren();
    setHidden("answer-reveal", active || !game.answer);
    if (!active && game.answer) {
      const person = game.answer;
      reveal.append(node("p", "eyebrow", game.status === "won" ? "恭喜识得此人 · 答案揭晓" : "史中之人 · 答案揭晓"), node("h3", "answer-name", person.name));
      reveal.append(node("p", "answer-meta", [yearLabel(person), ...(person.factions || []), ...(person.roles || []).slice(0, 2)].join(" · ")));
      if (person.summary) reveal.append(node("p", "answer-summary", person.summary));
      reveal.append(sourceLinks(person));
    }
    refreshDisabled();
    updateTimer();
    if (options.scroll !== false) conversation.scrollTop = conversation.scrollHeight;
    $("chat-tab-game").disabled = false;
    $("chat-tab-game").textContent = active ? "本局交流" : "本局复盘";
    if (oldId !== game.id) selectChat("game");
    else if (chatState.key === `game:${game.id}`) {
      renderChat();
      const session = chatSession();
      if (session.data?.mode === "active" && !active) loadChat(chatState.key, true);
    }
  }
  async function loadMeta() {
    try {
      const data = await api("/api/meta");
      state.meta = data;
      const agent = $("agent-status");
      agent.className = `agent-status ${data.agent?.ready ? "ready" : "unavailable"}`;
      agent.lastElementChild.textContent = data.agent?.ready ? "裁判已就绪" : "裁判尚未就绪";
      agent.title = data.agent?.detail || "";
      $("database-note").textContent = `${data.character_count || 0} 位人物在册 · 汉末至西晋`;
      (data.presets || []).forEach((preset) => {
        const radio = document.querySelector(`input[name="preset"][value="${preset.id === "sanguozhi" ? "sanguozhi" : "broad"}"]`);
        if (!radio) return;
        radio.parentElement.querySelector("strong").textContent = preset.name;
        radio.parentElement.querySelector("small").textContent = `${preset.count ?? ""} 位人物 · ${preset.id === "sanguozhi" ? "《三国志》有载" : "184—316 年间"}`;
        radio.parentElement.title = preset.description || "";
      });
      if (!data.agent?.ready) {
        notice("global-notice", data.agent?.detail || "裁判尚未就绪，请检查后台推理服务。", true);
        const retry = node("button", "text-button", "重新检查");
        retry.type = "button";
        retry.addEventListener("click", async () => {
          retry.disabled = true;
          retry.textContent = "正在连接…";
          try {
            await api("/api/agent/reconnect", { method: "POST", body: JSON.stringify({}) });
            await loadMeta();
          } catch (error) {
            notice("global-notice", error.message, true);
            retry.disabled = false;
            retry.textContent = "重新检查";
            $("global-notice").append(retry);
          }
        });
        $("global-notice").append(retry);
      } else notice("global-notice", "");
    } catch (error) {
      notice("global-notice", error.message, true);
      const retry = node("button", "text-button", "重新连接");
      retry.type = "button";
      retry.addEventListener("click", loadMeta);
      $("global-notice").append(retry);
    }
    refreshDisabled();
  }
  async function startGame(event) {
    event.preventDefault();
    if (state.busy) return;
    const body = { preset: document.querySelector("input[name=preset]:checked").value, scope: $("scope").value.trim(), question_limit: $("question-limit").value ? Number($("question-limit").value) : null, guess_limit: $("guess-limit").value ? Number($("guess-limit").value) : null, timer_enabled: $("timer-enabled").checked };
    notice("global-notice", "裁判正在核对范围、选择人物。首次开局可能需要稍等片刻…");
    notice("game-error", "");
    setBusy(true, "裁判正在为新的一局核对范围、选择人物…");
    $("start-button").firstElementChild.textContent = "正在请古人入局…";
    try {
      const game = await post("/api/games", body);
      renderGame(game);
      notice("global-notice", "");
      if (window.innerWidth <= 720) $("active-game").closest("section").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) { notice("global-notice", error.message, true); }
    finally { setBusy(false); }
  }
  async function submitTurn(kind, event) {
    event.preventDefault();
    if (state.busy || state.game?.status !== "active") return;
    const input = $(kind === "questions" ? "question-input" : "guess-input");
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    const signature = `${state.game.id}:${kind}:${text}`;
    if (state.pending?.signature !== signature) state.pending = { signature, request_id: requestId() };
    const body = { text, request_id: state.pending.request_id };
    notice("game-error", "");
    setBusy(true, kind === "questions" ? "裁判正在核对史料，稍候即有答案…" : "裁判正在核对你的猜测…");
    try {
      const game = await post(`/api/games/${encodeURIComponent(state.game.id)}/${kind}`, body);
      renderGame(game);
      input.value = "";
      state.pending = null;
    } catch (error) { notice("game-error", error.message, true); }
    finally {
      setBusy(false);
      if (!input.disabled) input.focus({ preventScroll: true });
    }
  }
  async function giveUp() {
    if (state.busy || state.game?.status !== "active") return;
    notice("game-error", "");
    setBusy(true, "正在揭晓史中之人…");
    try { renderGame(await post(`/api/games/${encodeURIComponent(state.game.id)}/give-up`, {})); }
    catch (error) { notice("game-error", error.message, true); }
    finally { setBusy(false); }
  }
  function chatSession(key = chatState.key) {
    if (!chatState.sessions.has(key)) chatState.sessions.set(key, { data: null, loaded: false, loading: false, busy: false, draft: "", pending: null, error: "", notice: "", revision: 0 });
    return chatState.sessions.get(key);
  }
  function chatGameId(key = chatState.key) { return key.startsWith("game:") ? key.slice(5) : null; }
  function currentChatMode() { return chatState.key === "scope" ? "scope" : state.game?.status === "active" ? "active" : "review"; }
  function selectChat(tab) {
    if (tab === "game" && !state.game) return;
    chatSession().draft = $("chat-input").value;
    chatState.key = tab === "game" ? `game:${state.game.id}` : "scope";
    const session = chatSession();
    $("chat-input").value = session.draft;
    renderChat();
    if (!session.loading && !session.busy) loadChat(chatState.key, true);
  }
  function fillChatDraft(text) {
    const session = chatSession();
    session.draft = text;
    $("chat-input").value = text;
    $("chat-input").focus({ preventScroll: true });
  }
  function validSuggestion(suggestion) {
    return suggestion && ["broad", "sanguozhi"].includes(suggestion.preset) && typeof suggestion.scope === "string" && suggestion.scope.length <= 500;
  }
  function applySuggestion(suggestion) {
    if (!validSuggestion(suggestion)) return;
    const radio = document.querySelector(`input[name="preset"][value="${suggestion.preset}"]`);
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
    $("scope").value = suggestion.scope;
    const session = chatSession();
    session.notice = "已填入开局设定。点击上方「请古人入局」即可使用这个范围。";
    renderChat();
  }
  function chatMessage(message, pending = false) {
    const user = message.role === "user";
    const item = node("article", `chat-message chat-message-${user ? "user" : "assistant"}${pending ? " chat-message-pending chat-draft-pending" : ""}`);
    item.append(node("span", "chat-avatar", user ? "你" : "问"));
    const body = node("div", "chat-message-body");
    const meta = node("div", "chat-message-meta");
    meta.append(node("span", "", user ? "你" : "问古"));
    if (message.created_at) {
      const time = node("time", "", readableDate(message.created_at));
      time.dateTime = message.created_at;
      meta.append(time);
    }
    body.append(meta, node("p", "chat-message-text", message.text));
    if (!user && validSuggestion(message.suggested_scope)) {
      const suggestion = node("div", "chat-suggestion");
      const description = node("div");
      description.append(node("small", "", "可用于下一局的范围"), node("p", "", `${presetName(message.suggested_scope.preset)}${message.suggested_scope.scope ? ` · ${message.suggested_scope.scope}` : " · 不额外缩小范围"}`));
      const adopt = node("button", "button button-outline button-small", "采用此范围");
      adopt.type = "button";
      adopt.addEventListener("click", () => applySuggestion(message.suggested_scope));
      suggestion.append(description, adopt);
      body.append(suggestion);
    }
    item.append(body);
    return item;
  }
  function renderChat(scroll = true) {
    const session = chatSession();
    const mode = currentChatMode();
    const isScope = mode === "scope";
    const gameTab = $("chat-tab-game");
    gameTab.disabled = !state.game;
    gameTab.textContent = state.game && state.game.status !== "active" ? "本局复盘" : "本局交流";
    document.querySelectorAll(".chat-tab").forEach((tab) => {
      const selected = tab.dataset.chatTab === (isScope ? "scope" : "game");
      tab.classList.toggle("selected", selected);
      tab.setAttribute("aria-selected", String(selected));
    });
    $("chat-content").setAttribute("aria-labelledby", isScope ? "chat-tab-scope" : "chat-tab-game");
    $("chat-context").textContent = isScope ? "开局前聊聊想猜的人物，也可以随时讨论下一局的范围。" : mode === "active" ? "当前这局还在推理中，可以聊玩法与规则；人物线索请在上方提问。" : `正在复盘${state.game?.answer?.name ? `「${state.game.answer.name}」这一局` : "当前这局"}，可自由讨论人物史实、逐条问答和判断依据。`;
    $("chat-input-label").textContent = isScope ? "聊聊你想要的范围" : mode === "active" ? "想了解哪些玩法？" : "这局有哪些想追问的地方？";
    $("chat-input").placeholder = isScope ? "例如：我想猜《三国志》里的人物，能帮我想一个适合入门的范围吗？" : mode === "active" ? "例如：是否问题可以包含两个条件吗？" : "例如：请逐条解释这局问答，指出判断依据和可能的错误。";
    $("chat-input").disabled = session.busy;
    $("chat-send").disabled = session.busy || session.loading;
    $("chat-send").firstChild.textContent = session.busy ? "回复中 " : "发送 ";
    $("chat-refresh").disabled = session.busy || session.loading;
    setHidden("chat-thinking", !session.busy);
    notice("chat-error", session.error);
    notice("chat-notice", session.notice);
    const messages = $("chat-messages");
    const oldScroll = messages.scrollTop;
    messages.replaceChildren();
    if (session.loading && !session.loaded) messages.append(node("p", "chat-loading", "正在取回这段对话…"));
    else if (!session.data?.messages?.length && !session.busy) {
      const empty = node("div", "chat-empty");
      empty.append(node("h3", "", isScope ? "先聊一聊，再请古人入局。" : mode === "active" ? "推理之外，也可从容交流。" : "答案已揭晓，故事还可以继续。"));
      empty.append(node("p", "", isScope ? "告诉我你的兴趣与熟悉程度，一起选定一个有趣的范围。" : mode === "active" ? "这里可以解释玩法，等揭晓后还能一起核对史实。" : "问问一条线索的来由，或让裁判完整复盘这次推理。"));
      const starters = node("div", "chat-starters");
      const examples = isScope ? ["帮我推荐一个适合入门的范围", "我想猜《三国志》里的女性人物"] : mode === "active" ? ["一次是否问题可以包含几个条件？", "猜人物时可以提交字号吗？"] : ["请逐条复盘这局问答", "这个人物有哪些值得了解的故事？"];
      examples.forEach((text) => {
        const button = node("button", "chat-starter", text);
        button.type = "button";
        button.addEventListener("click", () => fillChatDraft(text));
        starters.append(button);
      });
      empty.append(starters);
      messages.append(empty);
    }
    (session.data?.messages || []).forEach((message) => messages.append(chatMessage(message)));
    if (session.busy && session.pending) messages.append(chatMessage({ role: "user", text: session.pending.text }, true));
    messages.scrollTop = scroll ? messages.scrollHeight : oldScroll;
  }
  function checkedChatData(data, key) {
    if (!data || !Array.isArray(data.messages) || (data.game_id ?? null) !== chatGameId(key)) throw new Error("聊天记录格式异常，请刷新对话后重试。");
    return data;
  }
  async function loadChat(key = chatState.key, force = false) {
    const session = chatSession(key);
    if (session.busy || session.loading || (session.loaded && !force)) return;
    session.loading = true;
    session.error = "";
    const revision = ++session.revision;
    if (chatState.key === key) renderChat(false);
    try {
      const gameId = chatGameId(key);
      const data = checkedChatData(await api(`/api/chat${gameId ? `?game_id=${encodeURIComponent(gameId)}` : ""}`), key);
      if (session.revision === revision) { session.data = data; session.loaded = true; }
    } catch (error) { if (session.revision === revision) session.error = error.message; }
    finally {
      if (session.revision === revision) session.loading = false;
      if (chatState.key === key) renderChat();
    }
  }
  async function sendChat(event) {
    event?.preventDefault();
    const key = chatState.key;
    const session = chatSession(key);
    if (session.busy || session.loading) return;
    const text = $("chat-input").value.trim();
    if (!text) { $("chat-input").focus(); return; }
    const payload = { text, game_id: chatGameId(key), preset: document.querySelector("input[name=preset]:checked").value, scope: $("scope").value.trim() };
    const signature = JSON.stringify(payload);
    if (session.pending?.signature !== signature) session.pending = { ...payload, signature, request_id: requestId() };
    const body = { ...payload, request_id: session.pending.request_id };
    session.draft = text;
    $("chat-input").value = text;
    session.busy = true;
    session.error = "";
    session.notice = "";
    const revision = ++session.revision;
    renderChat();
    try {
      const data = checkedChatData(await post("/api/chat", body), key);
      if (session.revision === revision) {
        session.data = data;
        session.loaded = true;
        session.pending = null;
        if (session.draft === text) session.draft = "";
      }
    } catch (error) { if (session.revision === revision) session.error = error.message; }
    finally {
      if (session.revision === revision) session.busy = false;
      if (chatState.key === key) {
        $("chat-input").value = session.draft;
        renderChat();
        $("chat-input").focus({ preventScroll: true });
      }
    }
  }
  function characterCard(person) {
    const card = node("article", "character-card");
    const heading = node("div", "character-heading");
    heading.append(node("h2", "", person.name), node("span", "character-years", yearLabel(person)));
    card.append(heading);
    const tags = node("div", "character-tags");
    [...(person.factions || []), ...(person.roles || []).slice(0, 2)].forEach((tag) => tags.append(node("span", "tag", tag)));
    if (person.sanguozhi) tags.append(node("span", "tag red", "《三国志》有载"));
    if (tags.children.length) card.append(tags);
    card.append(node("p", "", person.summary || "暂无人物小传。"));
    const details = node("details");
    details.append(node("summary", "", "别名与史料"));
    if (person.aliases?.length) details.append(node("p", "", `别名 / 字号：${person.aliases.join("、")}`));
    if (person.sanguozhi_evidence?.quote) details.append(node("p", "", `《三国志》线索：${person.sanguozhi_evidence.quote}`));
    details.append(sourceLinks(person));
    card.append(details);
    return card;
  }
  async function loadLibrary(reset = false) {
    if (state.libraryLoading && !reset) return;
    const revision = ++state.libraryRevision;
    state.libraryLoading = true;
    if (reset) { state.libraryOffset = 0; $("character-grid").replaceChildren(node("p", "loading-placeholder", "正在翻开人物册…")); }
    $("library-more").disabled = true;
    notice("library-notice", "");
    const params = new URLSearchParams({ preset: $("library-preset").value, q: $("library-search").value.trim(), offset: String(state.libraryOffset), limit: "24" });
    try {
      const data = await api(`/api/characters?${params}`);
      if (revision !== state.libraryRevision) return;
      if (reset) $("character-grid").replaceChildren();
      state.libraryTotal = data.total;
      state.libraryOffset += data.items.length;
      $("library-more").textContent = "再翻一页";
      $("library-total").replaceChildren(document.createTextNode(String(data.total)), node("small", "", "位人物"));
      data.items.forEach((person) => $("character-grid").append(characterCard(person)));
      if (!data.total) $("character-grid").append(node("p", "list-empty", "暂未找到这位人物，换个名字或范围试试。"));
      setHidden("library-more", state.libraryOffset >= data.total);
    } catch (error) {
      if (revision !== state.libraryRevision) return;
      if (reset) $("character-grid").replaceChildren();
      notice("library-notice", error.message, true);
      setHidden("library-more", false);
      $("library-more").textContent = "重新加载";
    } finally {
      if (revision === state.libraryRevision) { state.libraryLoading = false; $("library-more").disabled = false; }
    }
  }
  function historyCard(game, index) {
    const card = node("article", "history-card");
    card.append(node("span", "history-card-index", String(index + 1).padStart(2, "0")));
    const content = node("div", "history-card-content");
    const title = node("div", "history-title");
    title.append(node("h2", "", game.status !== "active" && game.answer?.name ? game.answer.name : "尚未揭晓的人物"), node("span", `status-pill ${game.status}`, statuses[game.status] || game.status));
    content.append(title, node("p", "", `${readableDate(game.created_at)} · ${presetName(game.preset)} · ${game.question_count || 0} 次提问 / ${game.guess_count || 0} 次猜测`));
    if (game.scope) content.append(node("p", "history-scope", game.scope));
    card.append(content);
    const open = node("button", "button button-outline", game.status === "active" ? "继续推理 ↗" : "回看此局 ↗");
    open.type = "button";
    open.addEventListener("click", async () => {
      if (state.busy) { notice("history-notice", "裁判正在处理当前提交，请等答案返回后再切换游戏。"); return; }
      open.disabled = true;
      setBusy(true, "正在取回游戏记录…");
      try {
        const result = await api(`/api/games/${encodeURIComponent(game.id)}`);
        notice("game-error", "");
        renderGame(result);
        showView("play", true);
        if (window.innerWidth <= 720) $("active-game").closest("section").scrollIntoView({ behavior: "smooth" });
      } catch (error) { notice("history-notice", error.message, true); }
      finally { open.disabled = false; setBusy(false); }
    });
    card.append(open);
    return card;
  }
  async function loadHistory(reset = false) {
    if (state.historyLoading) return;
    state.historyLoading = true;
    if (reset) { state.historyOffset = 0; $("history-list").replaceChildren(node("p", "loading-placeholder", "正在查阅往局…")); }
    $("history-more").disabled = true;
    $("history-refresh").disabled = true;
    notice("history-notice", "");
    try {
      const data = await api(`/api/games?offset=${state.historyOffset}&limit=15`);
      if (reset) $("history-list").replaceChildren();
      data.items.forEach((game, index) => $("history-list").append(historyCard(game, state.historyOffset + index)));
      state.historyOffset += data.items.length;
      state.historyTotal = data.total;
      if (!data.total) $("history-list").append(node("p", "list-empty", "还没有往局。从第一问，开始你的历史推理。"));
      setHidden("history-more", state.historyOffset >= data.total);
    } catch (error) {
      if (reset) $("history-list").replaceChildren();
      notice("history-notice", error.message, true);
    } finally {
      state.historyLoading = false;
      $("history-more").disabled = false;
      $("history-refresh").disabled = false;
    }
  }
  async function init() {
    document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
    document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); showView("play", true); });
    window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
    document.querySelectorAll("input[name=preset]").forEach((radio) => radio.addEventListener("change", () => document.querySelectorAll(".preset-option").forEach((label) => label.classList.toggle("selected", label.querySelector("input").checked))));
    $("setup-form").addEventListener("submit", startGame);
    $("question-form").addEventListener("submit", (event) => submitTurn("questions", event));
    $("guess-form").addEventListener("submit", (event) => submitTurn("guesses", event));
    $("give-up-button").addEventListener("click", () => setHidden("give-up-confirm", false));
    $("give-up-cancel").addEventListener("click", () => setHidden("give-up-confirm", true));
    $("give-up-yes").addEventListener("click", giveUp);
    $("toggle-timer").addEventListener("click", () => {
      state.timerHidden = !state.timerHidden;
      if (state.game) storageSet(`${TIMER_KEY}:${state.game.id}`, state.timerHidden ? "1" : "0");
      updateTimer();
    });
    $("library-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadLibrary(true), 280); });
    $("library-preset").addEventListener("change", () => loadLibrary(true));
    $("library-more").addEventListener("click", () => loadLibrary(false));
    $("history-more").addEventListener("click", () => loadHistory(false));
    $("history-refresh").addEventListener("click", () => loadHistory(true));
    document.querySelectorAll(".chat-tab").forEach((tab) => tab.addEventListener("click", () => selectChat(tab.dataset.chatTab)));
    $("chat-form").addEventListener("submit", sendChat);
    $("chat-input").addEventListener("input", () => { chatSession().draft = $("chat-input").value; });
    $("chat-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        if (!$("chat-send").disabled) $("chat-form").requestSubmit();
      }
    });
    $("chat-refresh").addEventListener("click", () => loadChat(chatState.key, true));
    setInterval(updateTimer, 1000);
    setBusy(true);
    $("start-button").firstElementChild.textContent = "正在连接裁判…";
    showView(location.hash.slice(1) || "play");
    await loadMeta();
    const lastId = storageGet(LAST_GAME_KEY);
    if (lastId) {
      try { renderGame(await api(`/api/games/${encodeURIComponent(lastId)}`), { scroll: false }); }
      catch { /* A deleted database or expired browser session simply starts fresh. */ }
    }
    if (!state.game) selectChat("scope");
    setBusy(false);
  }
  init();
})();
