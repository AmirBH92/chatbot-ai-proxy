/**
 * Chatbot AI — CO LABS CONSEILS
 * Widget vanilla JS injectable dans le backend Odoo 19.3 via ir.asset.
 *
 * Architecture :
 *   Question → Proxy FastAPI (Claude) → Intent JSON
 *   Intent   → Odoo /json/2 (session browser) → Données
 *   Données  → Widget (tableau / graphique / compteur)
 */
(function () {
  "use strict";

  // ── Configuration ─────────────────────────────────────────────────────────
  const PROXY_URL  = window.__CHATBOT_PROXY_URL__ || "%%PROXY_URL%%";
  const ODOO_URL   = window.location.origin;

  const SUGGESTIONS = [
    "Combien de commandes ce mois-ci ?",
    "Liste des 10 dernières factures clients",
    "Chiffre d'affaires par client ce trimestre",
    "Mes contacts sans email",
    "Produits créés cette semaine",
  ];

  // Modèles à sonder (on filtre ceux disponibles au démarrage)
  const CANDIDATE_MODELS = [
    "sale.order", "sale.order.line",
    "account.move", "account.move.line",
    "res.partner", "product.template", "product.product",
    "res.users", "crm.lead", "purchase.order",
    "stock.picking", "project.task", "hr.employee",
  ];

  const DEFAULT_FIELDS = {
    "sale.order":      ["name", "partner_id", "date_order", "amount_total", "state"],
    "account.move":    ["name", "partner_id", "invoice_date", "amount_total", "state", "move_type"],
    "res.partner":     ["name", "email", "phone", "is_company"],
    "product.template":["name", "list_price", "categ_id", "type"],
    "crm.lead":        ["name", "partner_id", "expected_revenue", "stage_id", "probability"],
  };

  // ── State ──────────────────────────────────────────────────────────────────
  let state = {
    open: false,
    loading: false,
    messages: [],
    unread: 0,
    userName: "",
    availableModels: [],
    history: [],
    nextId: 1,
    chartInstances: {},
    initialized: false,
  };

  // ── CSS ────────────────────────────────────────────────────────────────────
  function injectCSS() {
    if (document.getElementById("chatbot-ai-css")) return;
    const style = document.createElement("style");
    style.id = "chatbot-ai-css";
    style.textContent = `
.cbai-fab{position:fixed;bottom:24px;right:24px;z-index:9999;width:52px;height:52px;border-radius:50%;background:#D4246E;border:none;box-shadow:0 4px 16px rgba(212,36,110,.45);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:transform .2s,box-shadow .2s;}
.cbai-fab:hover{transform:scale(1.08);box-shadow:0 6px 22px rgba(212,36,110,.6);}
.cbai-fab svg{width:24px;height:24px;fill:#fff;}
.cbai-badge{position:absolute;top:-3px;right:-3px;background:#F97316;color:#fff;font-size:9px;font-weight:700;border-radius:10px;padding:1px 5px;min-width:16px;text-align:center;line-height:14px;}
.cbai-panel{position:fixed;bottom:84px;right:24px;z-index:9998;width:420px;max-height:76vh;border-radius:14px;background:#0C1226;border:1px solid #1A2B48;box-shadow:0 12px 48px rgba(0,0,0,.5);display:flex;flex-direction:column;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;animation:cbai-in .2s ease;}
@keyframes cbai-in{from{opacity:0;transform:translateY(12px) scale(.97)}to{opacity:1;transform:none}}
.cbai-head{display:flex;align-items:center;gap:10px;padding:14px 16px;background:#111B35;border-bottom:1px solid #1A2B48;flex-shrink:0;}
.cbai-avatar{width:32px;height:32px;border-radius:50%;background:#D4246E;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;}
.cbai-title{flex:1;}.cbai-title strong{display:block;color:#EEF2FF;font-size:13px;font-weight:600;}
.cbai-title small{color:#7A9BC4;font-size:10px;}
.cbai-close{background:none;border:none;color:#3E5278;cursor:pointer;padding:4px;border-radius:4px;font-size:16px;line-height:1;transition:color .15s;}
.cbai-close:hover{color:#EEF2FF;}
.cbai-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px;min-height:0;}
.cbai-msgs::-webkit-scrollbar{width:4px;}.cbai-msgs::-webkit-scrollbar-thumb{background:#1A2B48;border-radius:4px;}
.cbai-msg{max-width:88%;line-height:1.45;font-size:13px;}
.cbai-msg.user{align-self:flex-end;background:#1A2B48;color:#EEF2FF;padding:10px 14px;border-radius:12px 12px 4px 12px;}
.cbai-msg.bot{align-self:flex-start;background:#111B35;color:#CFE2F6;padding:10px 14px;border-radius:12px 12px 12px 4px;border:1px solid #1A2B48;}
.cbai-msg.bot.err{border-color:#F97316;color:#F97316;}
.cbai-big{text-align:center;padding:16px 0 8px;}.cbai-big .n{font-size:42px;font-weight:700;color:#D4246E;line-height:1;}
.cbai-big .lbl{font-size:11px;color:#7A9BC4;margin-top:4px;}
.cbai-table-wrap{overflow:auto;max-height:200px;border-radius:6px;border:1px solid #1A2B48;margin-top:6px;}
.cbai-table-wrap::-webkit-scrollbar{height:4px;width:4px;}.cbai-table-wrap::-webkit-scrollbar-thumb{background:#1A2B48;}
.cbai-table{width:100%;border-collapse:collapse;font-size:11.5px;}
.cbai-table th{background:#1A2B48;color:#7A9BC4;padding:5px 8px;text-align:left;font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #0C1226;}
.cbai-table td{padding:5px 8px;color:#CFE2F6;border-bottom:1px solid #1A2B48;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.cbai-table tr:last-child td{border-bottom:none;}.cbai-table tr:hover td{background:rgba(26,43,72,.5);}
.cbai-chart{width:100%;height:180px;position:relative;}
.cbai-typing{align-self:flex-start;display:flex;gap:4px;padding:10px 14px;background:#111B35;border:1px solid #1A2B48;border-radius:12px 12px 12px 4px;}
.cbai-typing span{width:6px;height:6px;border-radius:50%;background:#7A9BC4;animation:cbai-bounce 1.2s infinite;}
.cbai-typing span:nth-child(2){animation-delay:.2s;}.cbai-typing span:nth-child(3){animation-delay:.4s;}
@keyframes cbai-bounce{0%,80%,100%{transform:translateY(0);opacity:.5}40%{transform:translateY(-5px);opacity:1}}
.cbai-suggs{display:flex;flex-wrap:wrap;gap:6px;padding:0 0 4px;}
.cbai-sugg{background:#111B35;border:1px solid #1A2B48;color:#7A9BC4;font-size:11px;padding:5px 10px;border-radius:16px;cursor:pointer;transition:border-color .15s,color .15s;}
.cbai-sugg:hover{border-color:#D4246E;color:#EEF2FF;}
.cbai-foot{padding:12px 14px;border-top:1px solid #1A2B48;background:#0C1226;flex-shrink:0;}
.cbai-row{display:flex;gap:8px;align-items:flex-end;}
.cbai-input{flex:1;background:#111B35;border:1px solid #1A2B48;border-radius:10px;color:#EEF2FF;font-size:13px;padding:9px 12px;resize:none;outline:none;transition:border-color .15s;font-family:inherit;line-height:1.4;max-height:100px;min-height:38px;}
.cbai-input::placeholder{color:#3E5278;}.cbai-input:focus{border-color:#4B8FD4;}
.cbai-send{background:#D4246E;border:none;border-radius:10px;color:#fff;cursor:pointer;padding:9px 14px;font-size:15px;transition:background .15s;flex-shrink:0;height:38px;display:flex;align-items:center;justify-content:center;}
.cbai-send:hover:not(:disabled){background:#b01d5c;}.cbai-send:disabled{background:#3E5278;cursor:not-allowed;}
.cbai-hint{font-size:10px;color:#3E5278;margin-top:6px;text-align:center;}
    `;
    document.head.appendChild(style);
  }

  // ── DOM helpers ───────────────────────────────────────────────────────────
  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html) e.innerHTML = html;
    return e;
  }

  function fmt(v) {
    if (typeof v !== "number") return String(v ?? "");
    return new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 2 }).format(v);
  }

  // ── Odoo API (browser session) ────────────────────────────────────────────
  async function odooCall(model, method, kw = {}) {
    const resp = await fetch(`${ODOO_URL}/json/2/${model}/${method}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(kw),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  // ── Initialisation : sonder modèles disponibles + user ───────────────────
  async function init() {
    if (state.initialized) return;
    state.initialized = true;

    // Récupérer nom utilisateur
    try {
      const users = await odooCall("res.users", "search_read", {
        domain: [["id", "=", "__current_user__"]],
        fields: ["name"],
        limit: 1,
      });
      if (!users.length) {
        // Fallback : lire depuis session
        const me = await odooCall("res.users", "read", { ids: [], fields: ["name"] });
        if (me.length) state.userName = me[0].name;
      } else {
        state.userName = users[0].name;
      }
    } catch (_) {}

    // Tenter d'obtenir l'utilisateur actuel via session_info
    if (!state.userName) {
      try {
        const si = await fetch(`${ODOO_URL}/web/session/get_session_info`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {} }),
        });
        const sdata = await si.json();
        if (sdata.result?.name) state.userName = sdata.result.name;
      } catch (_) {}
    }

    // Sonder les modèles disponibles
    const available = [];
    for (const m of CANDIDATE_MODELS) {
      try {
        await odooCall(m, "search_count", { domain: [] });
        available.push(m);
      } catch (_) {}
    }
    state.availableModels = available;

    renderWelcome();
  }

  // ── Execute intent Odoo ───────────────────────────────────────────────────
  async function executeIntent(intent) {
    const type      = intent.type || "query";
    const modelName = intent.model || "";

    if (type === "message") {
      return { type: "message", content: intent.message || "" };
    }

    if (!modelName) return { type: "error", content: "Modèle non spécifié." };

    const domain = intent.domain || [];

    // COUNT
    if (type === "count") {
      const count = await odooCall(modelName, "search_count", { domain });
      return { type: "count", value: count, label: modelName };
    }

    // SUM
    if (type === "sum") {
      const field   = intent.aggregation?.field || "amount_total";
      const records = await odooCall(modelName, "search_read", {
        domain,
        fields: [field],
        limit: 500,
      });
      const total = records.reduce((acc, r) => acc + (r[field] || 0), 0);
      return { type: "sum", value: total, field };
    }

    // CHART
    if (type === "chart") {
      const groupby   = intent.groupby || [];
      const aggField  = intent.aggregation?.field || "amount_total";
      const labelField = intent.chart_label_field || (groupby[0] || "id");

      if (!groupby.length) {
        intent.type = "query";
        return executeIntent(intent);
      }

      const groups = await odooCall(modelName, "read_group", {
        domain,
        fields: [aggField],
        groupby,
        lazy: false,
      });

      const labels = [], values = [];
      for (const g of groups.slice(0, 30)) {
        let lbl = g[labelField] ?? "N/A";
        if (Array.isArray(lbl)) lbl = lbl[1] ?? lbl[0];
        labels.push(String(lbl));
        values.push(Math.round((g[aggField] || 0) * 100) / 100);
      }

      return {
        type: "chart",
        chart_type: intent.chart_type || "bar",
        labels,
        values,
        value_label: aggField,
        id: state.nextId++,
      };
    }

    // QUERY (liste)
    const fields  = intent.fields || DEFAULT_FIELDS[modelName] || ["name"];
    const orderby = intent.orderby || "id desc";
    const limit   = Math.min(parseInt(intent.limit || 40, 10), 100);

    const records = await odooCall(modelName, "search_read", {
      domain,
      fields,
      order: orderby,
      limit,
    });

    // Formater many2one
    const formatted = records.map((rec) => {
      const row = {};
      for (const [k, v] of Object.entries(rec)) {
        if (Array.isArray(v) && v.length === 2 && typeof v[0] === "number") {
          row[k] = v[1];
        } else if (v === false) {
          row[k] = "";
        } else {
          row[k] = v;
        }
      }
      return row;
    });

    return { type: "query", model: modelName, fields, records: formatted, total: formatted.length };
  }

  // ── Send message ──────────────────────────────────────────────────────────
  async function sendMessage() {
    const input = document.getElementById("cbai-input");
    if (!input) return;
    const question = input.value.trim();
    if (!question || state.loading) return;

    addMessage({ role: "user", type: "user", content: question, id: state.nextId++ });
    state.history.push({ role: "user", content: question });
    input.value = "";
    input.style.height = "auto";
    state.loading = true;
    renderTyping(true);

    try {
      // Appel proxy → intent Claude
      const proxyResp = await fetch(`${PROXY_URL}/api/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          available_models: state.availableModels,
          conversation_history: state.history.slice(-6),
        }),
      });

      if (!proxyResp.ok) {
        let errMsg = `Erreur ${proxyResp.status}`;
        try {
          const errBody = await proxyResp.json();
          errMsg = errBody.detail || errMsg;
        } catch (_) {}
        throw new Error(errMsg);
      }

      const intent = await proxyResp.json();
      renderTyping(false);
      state.loading = false;

      if (intent.error) {
        addMessage({ role: "bot", type: "error", content: intent.error, id: state.nextId++ });
        return;
      }

      // Exécuter intent contre Odoo
      let result;
      try {
        result = await executeIntent(intent);
      } catch (ormErr) {
        result = { type: "error", content: `Erreur Odoo : ${ormErr.message}` };
      }

      const msgId = state.nextId++;
      addMessage({ role: "bot", id: msgId, ...result });
      state.history.push({ role: "assistant", content: summarize(result) });

      if (!state.open) {
        state.unread++;
        updateBadge();
      }

    } catch (err) {
      renderTyping(false);
      state.loading = false;
      addMessage({ role: "bot", type: "error", content: `Erreur connexion : ${err.message}`, id: state.nextId++ });
    }
  }

  function summarize(r) {
    if (r.type === "count")   return `${r.value} enregistrement(s).`;
    if (r.type === "sum")     return `Total ${r.field} : ${r.value}.`;
    if (r.type === "query")   return `${r.total} enregistrement(s).`;
    if (r.type === "chart")   return `Graphique : ${r.labels?.length} données.`;
    return r.content || "";
  }

  // ── Rendu ──────────────────────────────────────────────────────────────────
  function addMessage(msg) {
    state.messages.push(msg);
    const container = document.getElementById("cbai-msgs");
    if (!container) return;
    appendMessageEl(container, msg);
    container.scrollTop = container.scrollHeight;
  }

  function appendMessageEl(container, msg) {
    if (msg.role === "user") {
      const d = el("div", "cbai-msg user");
      d.textContent = msg.content;
      container.appendChild(d);
      return;
    }

    // Bot messages
    if (msg.type === "error") {
      const d = el("div", "cbai-msg bot err");
      d.textContent = msg.content;
      container.appendChild(d);
      return;
    }

    if (msg.type === "message") {
      const d = el("div", "cbai-msg bot");
      d.textContent = msg.content;
      container.appendChild(d);
      return;
    }

    if (msg.type === "count") {
      const d = el("div", "cbai-msg bot");
      d.innerHTML = `<div class="cbai-big"><div class="n">${msg.value}</div><div class="lbl">${msg.label || ""}</div></div>`;
      container.appendChild(d);
      return;
    }

    if (msg.type === "sum") {
      const d = el("div", "cbai-msg bot");
      d.innerHTML = `<div class="cbai-big"><div class="n">${fmt(msg.value)}</div><div class="lbl">${msg.field || ""}</div></div>`;
      container.appendChild(d);
      return;
    }

    if (msg.type === "query") {
      const d = el("div", "cbai-msg bot");
      d.style.cssText = "max-width:100%;padding:10px 10px 8px;";
      const info = el("div");
      info.style.cssText = "font-size:10px;color:#7A9BC4;margin-bottom:4px;";
      info.textContent = `${msg.total} résultat(s)`;
      const wrap = el("div", "cbai-table-wrap");
      const table = el("table", "cbai-table");
      // Header
      const thead = el("thead");
      const tr = el("tr");
      for (const f of msg.fields) {
        const th = el("th");
        th.textContent = f;
        tr.appendChild(th);
      }
      thead.appendChild(tr);
      table.appendChild(thead);
      // Body
      const tbody = el("tbody");
      for (const rec of msg.records) {
        const row = el("tr");
        for (const f of msg.fields) {
          const td = el("td");
          td.textContent = rec[f] != null ? String(rec[f]) : "";
          td.title = td.textContent;
          row.appendChild(td);
        }
        tbody.appendChild(row);
      }
      table.appendChild(tbody);
      wrap.appendChild(table);
      d.appendChild(info);
      d.appendChild(wrap);
      container.appendChild(d);
      return;
    }

    if (msg.type === "chart") {
      const d = el("div", "cbai-msg bot");
      d.style.cssText = "max-width:100%;padding:10px;";
      const chartDiv = el("div", "cbai-chart");
      const canvas = el("canvas");
      canvas.id = `cbai-chart-${msg.id}`;
      chartDiv.appendChild(canvas);
      d.appendChild(chartDiv);
      container.appendChild(d);
      // Rendre le graphique après insertion DOM
      setTimeout(() => renderChart(msg), 50);
      return;
    }
  }

  function renderWelcome() {
    const container = document.getElementById("cbai-msgs");
    if (!container) return;
    const d = el("div", "cbai-msg bot");
    const greeting = state.userName ? `Bonjour ${state.userName}&nbsp;!` : "Bonjour&nbsp;!";
    let html = `${greeting} Posez-moi une question sur vos données Odoo :<br><br><div class="cbai-suggs">`;
    for (const s of SUGGESTIONS) {
      html += `<button class="cbai-sugg" onclick="document.getElementById('cbai-input').value=this.textContent;document.getElementById('cbai-input').focus();">${s}</button>`;
    }
    html += `</div>`;
    d.innerHTML = html;
    container.appendChild(d);
  }

  function renderTyping(show) {
    const existing = document.getElementById("cbai-typing");
    if (show && !existing) {
      const container = document.getElementById("cbai-msgs");
      if (!container) return;
      const d = el("div", "cbai-typing");
      d.id = "cbai-typing";
      d.innerHTML = "<span></span><span></span><span></span>";
      container.appendChild(d);
      container.scrollTop = container.scrollHeight;
    } else if (!show && existing) {
      existing.remove();
    }
  }

  function renderChart(msg) {
    const canvas = document.getElementById(`cbai-chart-${msg.id}`);
    if (!canvas || !window.Chart) {
      if (canvas) canvas.parentElement.innerHTML = '<p style="color:#7A9BC4;font-size:11px;text-align:center;">Chart.js non disponible</p>';
      return;
    }
    if (state.chartInstances[msg.id]) return;

    const palette = ["#D4246E","#4B8FD4","#22C55E","#F97316","#A855F7","#CFE2F6","#F59E0B","#06B6D4"];
    const n = msg.labels?.length || 1;
    const bg = Array.from({ length: n }, (_, i) => palette[i % palette.length] + "CC");
    const border = Array.from({ length: n }, (_, i) => palette[i % palette.length]);

    state.chartInstances[msg.id] = new window.Chart(canvas, {
      type: msg.chart_type || "bar",
      data: {
        labels: msg.labels || [],
        datasets: [{ label: msg.value_label || "Valeur", data: msg.values || [], backgroundColor: bg, borderColor: border, borderWidth: 1.5 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => " " + fmt(c.parsed.y ?? c.parsed) } } },
        scales: msg.chart_type === "pie" ? {} : {
          x: { ticks: { color: "#7A9BC4", font: { size: 10 } }, grid: { color: "#1A2B48" } },
          y: { ticks: { color: "#7A9BC4", font: { size: 10 }, callback: fmt }, grid: { color: "#1A2B48" } },
        },
      },
    });
  }

  function updateBadge() {
    const badge = document.getElementById("cbai-badge");
    if (!badge) return;
    if (state.unread > 0) {
      badge.textContent = state.unread;
      badge.style.display = "block";
    } else {
      badge.style.display = "none";
    }
  }

  // ── Build DOM ──────────────────────────────────────────────────────────────
  function buildUI() {
    if (document.getElementById("cbai-root")) return;

    const root = el("div");
    root.id = "cbai-root";

    // FAB
    const fab = el("button", "cbai-fab");
    fab.id = "cbai-fab";
    fab.title = "Chatbot AI";
    fab.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12c0 1.54.36 3 1 4.3L2 22l5.7-1A9.95 9.95 0 0012 22c5.52 0 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg><span id="cbai-badge" class="cbai-badge" style="display:none">0</span>`;
    fab.addEventListener("click", togglePanel);
    root.appendChild(fab);

    // Panel
    const panel = el("div", "cbai-panel");
    panel.id = "cbai-panel";
    panel.style.display = "none";

    // Header
    const head = el("div", "cbai-head");
    head.innerHTML = `<div class="cbai-avatar">🤖</div><div class="cbai-title"><strong>Chatbot AI</strong><small>Posez vos questions en langage naturel</small></div>`;
    const closeBtn = el("button", "cbai-close");
    closeBtn.textContent = "✕";
    closeBtn.addEventListener("click", togglePanel);
    head.appendChild(closeBtn);
    panel.appendChild(head);

    // Messages
    const msgs = el("div", "cbai-msgs");
    msgs.id = "cbai-msgs";
    panel.appendChild(msgs);

    // Footer
    const foot = el("div", "cbai-foot");
    const row = el("div", "cbai-row");
    const input = el("textarea", "cbai-input");
    input.id = "cbai-input";
    input.rows = 1;
    input.placeholder = "Posez votre question...";
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 100) + "px";
    });
    const sendBtn = el("button", "cbai-send");
    sendBtn.id = "cbai-send";
    sendBtn.textContent = "➤";
    sendBtn.addEventListener("click", sendMessage);
    row.appendChild(input);
    row.appendChild(sendBtn);
    foot.appendChild(row);
    foot.appendChild(el("div", "cbai-hint", "Entrée pour envoyer · Maj+Entrée pour nouvelle ligne"));
    panel.appendChild(foot);

    root.appendChild(panel);
    document.body.appendChild(root);
  }

  function togglePanel() {
    state.open = !state.open;
    const panel = document.getElementById("cbai-panel");
    if (panel) panel.style.display = state.open ? "flex" : "none";
    if (state.open) {
      state.unread = 0;
      updateBadge();
      document.getElementById("cbai-input")?.focus();
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  function boot() {
    if (PROXY_URL.startsWith("%%")) {
      console.warn("[Chatbot AI] PROXY_URL not configured. Set window.__CHATBOT_PROXY_URL__ before loading this script.");
    }
    injectCSS();
    buildUI();
    init();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
