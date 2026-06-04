/* Mangrove strategy sidebar — polls agent API, renders grouped accordion */
(function () {
  "use strict";

  const API_KEY   = "dev-key-1";
  const AGENT_URL = "http://localhost:9080";
  const POLL_MS   = 10_000;

  const STATUS_ICON = { live: "🟢", paper: "🔵", draft: "🟡", inactive: "⚫", archived: "⚫" };
  const EVAL_LABEL  = { ok: "✅", error: "❌", skipped: "⏭️" };
  const STATUS_ORDER = { live: 0, paper: 1, draft: 2, inactive: 3, archived: 4 };

  // Track which asset sections and strategy cards are open so re-renders
  // don't reset the user's expanded state
  const openSections  = new Set(); // asset names
  const openCards     = new Set(); // strategy ids
  let   lastDataHash  = null;

  // ── DOM setup ─────────────────────────────────────────────────────────

  function mount() {
    const tab = document.createElement("button");
    tab.id = "mgv-tab";
    tab.title = "Toggle strategy panel";
    tab.innerHTML = "Strategies";

    const sidebar = document.createElement("div");
    sidebar.id = "mgv-sidebar";
    sidebar.innerHTML = `
      <h2 style="margin:0 0 4px">📊 Strategies</h2>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
        <span id="mgv-sidebar-meta" style="margin:0">Loading…</span>
        <button id="mgv-refresh" title="Refresh"
          style="background:#2563eb;border:none;color:#fff;font-size:15px;font-weight:700;cursor:pointer;padding:2px 8px;border-radius:6px;line-height:1.4;flex-shrink:0"><span class="mgv-refresh-icon">↻</span></button>
      </div>
      <div id="mgv-list"></div>
    `;

    document.body.appendChild(tab);
    document.body.appendChild(sidebar);

    tab.addEventListener("click", () => sidebar.classList.toggle("open"));

    document.addEventListener("click", (e) => {
      if (sidebar.classList.contains("open") &&
          !sidebar.contains(e.target) &&
          e.target !== tab) {
        sidebar.classList.remove("open");
      }
    });
    sidebar.querySelector("#mgv-refresh").addEventListener("click", (e) => {
      e.stopPropagation();
      const btn = e.currentTarget;
      btn.classList.remove("spinning");
      void btn.offsetWidth; // force reflow so re-adding the class restarts animation
      btn.classList.add("spinning");
      btn.addEventListener("animationend", () => btn.classList.remove("spinning"), { once: true });
      lastDataHash = null;
      refresh();
    });
  }

  // ── API helpers ───────────────────────────────────────────────────────

  async function fetchJSON(path) {
    const r = await fetch(AGENT_URL + path, { headers: { "X-API-Key": API_KEY } });
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    return r.json();
  }

  async function fetchLastEval(strategyId) {
    try {
      const rows = await fetchJSON(`/api/v1/agent/strategies/${strategyId}/evaluations?limit=1`);
      return rows[0] ?? null;
    } catch { return null; }
  }

  // ── Render helpers ────────────────────────────────────────────────────

  function evalLine(ev) {
    if (!ev) return '<span style="color:#475569">No evaluations yet</span>';
    const ts       = ev.timestamp.slice(0, 16).replace("T", " ");
    const icon     = EVAL_LABEL[ev.status] ?? "❓";
    const trades   = ev.order_intents?.length ?? 0;
    const tradeStr = trades ? `${trades} trade${trades !== 1 ? "s" : ""}` : "no trades";
    return `<span class="mgv-eval-${ev.status}">${icon} ${ts} — ${tradeStr}</span>`;
  }

  function renderCard(s, ev) {
    const card = document.createElement("div");
    card.className = "mgv-card" + (openCards.has(s.id) ? " open" : "");
    card.dataset.id = s.id;

    card.innerHTML = `
      <div class="mgv-card-header">
        <span class="mgv-card-name">${STATUS_ICON[s.status] ?? "⚪"} ${s.name}</span>
        <span class="mgv-badge mgv-badge-${s.status}">${s.status}</span>
        <span class="mgv-chevron">▼</span>
      </div>
      <div class="mgv-card-body">
        <div>${[s.timeframe].filter(Boolean).join(" · ")}</div>
        <div style="margin-top:5px">${evalLine(ev)}</div>
      </div>
    `;

    card.querySelector(".mgv-card-header").addEventListener("click", () => {
      card.classList.toggle("open");
      if (card.classList.contains("open")) openCards.add(s.id);
      else openCards.delete(s.id);
    });

    return card;
  }

  function renderSection(asset, strategies, evals) {
    const label    = asset || "Other";
    const isOpen   = openSections.has(label);
    const active   = strategies.filter(s => s.status === "live" || s.status === "paper").length;
    const statusDot = active > 0 ? "🔵" : "⚫";

    const section = document.createElement("div");
    section.className = "mgv-section" + (isOpen ? " open" : "");
    section.dataset.asset = label;

    const cardsHtml = document.createElement("div");
    cardsHtml.className = "mgv-section-body";
    strategies.forEach(s => cardsHtml.appendChild(renderCard(s, evals[s.id])));

    section.innerHTML = `
      <div class="mgv-section-header">
        <span class="mgv-section-title">${statusDot} ${label}</span>
        <span class="mgv-section-count">${strategies.length}</span>
        <span class="mgv-chevron">▼</span>
      </div>
    `;
    section.appendChild(cardsHtml);

    section.querySelector(".mgv-section-header").addEventListener("click", () => {
      section.classList.toggle("open");
      if (section.classList.contains("open")) openSections.add(label);
      else openSections.delete(label);
    });

    return section;
  }

  // ── Poll & refresh ────────────────────────────────────────────────────

  async function refresh() {
    const list = document.getElementById("mgv-list");
    const meta = document.getElementById("mgv-sidebar-meta");
    if (!list || !meta) return;

    try {
      const strategies = await fetchJSON("/api/v1/agent/strategies");

      // Fetch all evals in parallel
      const evals = {};
      await Promise.all(strategies.map(async s => {
        evals[s.id] = await fetchLastEval(s.id);
      }));

      // Only re-render if data actually changed
      const hash = JSON.stringify(strategies.map(s => ({
        id: s.id, status: s.status, name: s.name,
        ev: evals[s.id]?.timestamp ?? null,
        ev_trades: evals[s.id]?.order_intents?.length ?? 0,
      })));
      if (hash === lastDataHash) return;
      lastDataHash = hash;

      // Update meta
      if (!strategies.length) {
        list.innerHTML = '<div class="mgv-empty">No strategies yet.<br>Ask Sage to build one.</div>';
        meta.textContent = "0 strategies";
        return;
      }
      const active = strategies.filter(s => s.status === "live" || s.status === "paper").length;
      meta.textContent = `${strategies.length} total · ${active} active`;

      // Sort within each group: live → paper → draft → inactive → archived
      strategies.sort((a, b) => (STATUS_ORDER[a.status] ?? 5) - (STATUS_ORDER[b.status] ?? 5));

      // Group by asset
      const groups = {};
      for (const s of strategies) {
        const key = s.asset || "Other";
        if (!groups[key]) groups[key] = [];
        groups[key].push(s);
      }

      // Render — asset groups sorted: named assets first, "Other" last
      list.innerHTML = "";
      const assets = Object.keys(groups).sort((a, b) => {
        if (a === "Other") return 1;
        if (b === "Other") return -1;
        return a.localeCompare(b);
      });

      for (const asset of assets) {
        list.appendChild(renderSection(asset, groups[asset], evals));
      }

    } catch {
      if (meta) meta.textContent = "Agent server unreachable";
    }
  }

  // ── Init ──────────────────────────────────────────────────────────────

  // ── Branding ──────────────────────────────────────────────────────────

  function replaceBranding() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode()) {
      if (walker.currentNode.textContent.toLowerCase().includes("chainlit")) {
        hits.push(walker.currentNode);
      }
    }
    hits.forEach(node => {
      node.textContent = node.textContent.replace(/chainlit/gi, "Mangrove · Sage");
      const p = node.parentElement;
      if (p && p.tagName === "A") {
        p.removeAttribute("href");
        p.style.cssText += "cursor:default;text-decoration:none;pointer-events:none;";
      }
    });
  }

  const _brandObserver = new MutationObserver(replaceBranding);

  function init() {
    mount();
    refresh();
    setInterval(refresh, POLL_MS);
    _brandObserver.observe(document.body, { childList: true, subtree: true });
    replaceBranding();
    setTimeout(replaceBranding, 1000);
    setTimeout(replaceBranding, 3000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    setTimeout(init, 500);
  }
})();
