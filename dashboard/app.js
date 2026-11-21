/* Renders the dashboard from window.DASHBOARD_DATA (written by the agent). */
(function () {
  const D = window.DASHBOARD_DATA;
  if (!D) {
    document.querySelector("main").innerHTML =
      '<div class="card"><p class="empty">No data yet — run TrendPilot once (python -m src.main) to generate data.js.</p></div>';
    return;
  }

  /* ---------- helpers ---------- */
  const $ = (id) => document.getElementById(id);
  const money = (n) =>
    n == null ? "—" : "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = (n, digits = 2) => {
    if (n == null || isNaN(n)) return "—";
    const v = Number(n);
    return (v > 0 ? "+" : "") + v.toFixed(digits) + "%";
  };
  const pctClass = (n) => (n == null ? "muted" : Number(n) >= 0 ? "pos" : "neg");
  const mcap = (n) => {
    if (!n) return "—";
    if (n >= 1e12) return (n / 1e12).toFixed(2) + "T";
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
    return (n / 1e6).toFixed(0) + "M";
  };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function table(headers, rows) {
    if (!rows.length) return '<p class="empty">No data available.</p>';
    return (
      "<table><thead><tr>" + headers.map((h) => `<th>${h}</th>`).join("") +
      "</tr></thead><tbody>" + rows.map((r) => "<tr>" + r.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("") +
      "</tbody></table>"
    );
  }

  function newsList(el, articles) {
    if (!articles || !articles.length) { el.innerHTML = '<p class="empty">No articles.</p>'; return; }
    el.innerHTML = articles
      .map((a) => {
        const title = a.url
          ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>`
          : `<span>${esc(a.title)}</span>`;
        return `<div class="news-item">${title}<div class="src">${esc(a.source || "")}</div>` +
          (a.description ? `<div class="desc">${esc(a.description)}</div>` : "") + "</div>";
      })
      .join("");
  }

  // Extract a "## Section" chunk out of the report markdown.
  function mdSection(name) {
    // Guarded: a data.js written by a run that failed before the narrative
    // existed has no report_markdown, and this used to throw and blank the page.
    const source = D.report_markdown || "";
    if (!source) return null;
    const re = new RegExp("^##\\s+" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*$", "mi");
    const m = source.match(re);
    if (!m) return null;
    const start = m.index + m[0].length;
    const rest = source.slice(start);
    const next = rest.search(/^##\s+/m);
    return rest.slice(0, next === -1 ? undefined : next).trim();
  }
  const mdRender = (text) => (text ? marked.parse(text) : '<p class="empty">Not in today\'s report.</p>');

  /* ---------- header ---------- */
  $("meta-line").textContent = `Report date: ${D.run_date} · Generated ${D.generated_at}`;

  /* ---------- tabs ---------- */
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $("page-" + btn.dataset.page).classList.add("active");
    });
  });

  /* ---------- portfolio (rendered as a function so Refresh can re-call it) ---------- */
  const palette = ["#4ade80", "#60a5fa", "#fbbf24", "#f87171", "#b8a5f5", "#34d399", "#f472b6", "#93c5fd"];
  let allocationChart = null;
  let networthChart = null;

  // If the chart library failed to load, skip the charts rather than let the
  // exception stop every section below from rendering.
  const chartsOk = typeof Chart !== "undefined";

  function renderPortfolio(P) {
    $("header-networth").textContent = money(P.net_worth);

    const hist = P.history || [];
    let histChange = null;
    if (hist.length >= 2) {
      const first = hist[0].net_worth, last = hist[hist.length - 1].net_worth;
      if (first) histChange = ((last - first) / first) * 100;
    }
    $("portfolio-stats").innerHTML = [
      { k: "Net Worth", v: money(P.net_worth) },
      { k: "Cash", v: money(P.cash_total) },
      { k: "Invested (Holdings)", v: money(P.holdings_value) },
      { k: "Change Since Tracking Began", v: pct(histChange), cls: pctClass(histChange) },
    ]
      .map((s) => `<div class="stat"><div class="k">${s.k}</div><div class="v ${s.cls || ""}">${s.v}</div></div>`)
      .join("");

    if (chartsOk) renderCharts(P, hist);
    renderTables(P);
  }

  function renderCharts(P, hist) {
    if (allocationChart) allocationChart.destroy();
    allocationChart = new Chart($("allocation-chart"), {
      type: "doughnut",
      data: {
        labels: P.allocation.map((a) => a.label),
        datasets: [{ data: P.allocation.map((a) => a.value), backgroundColor: palette, borderColor: "#161b22", borderWidth: 2 }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "right", labels: { color: "#e6edf3", padding: 14 } },
          tooltip: { callbacks: { label: (ctx) => ` ${ctx.label}: ${money(ctx.parsed)}` } },
        },
      },
    });

    if (networthChart) networthChart.destroy();
    networthChart = new Chart($("networth-chart"), {
      type: "line",
      data: {
        labels: hist.map((h) => h.date),
        datasets: [{
          data: hist.map((h) => h.net_worth),
          borderColor: "#4ade80",
          backgroundColor: "rgba(74, 222, 128, 0.12)",
          fill: true,
          tension: 0.25,
          pointRadius: 3,
        }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => money(ctx.parsed.y) } } },
        scales: {
          x: { ticks: { color: "#8b98a9" }, grid: { color: "#2a3242" } },
          y: { ticks: { color: "#8b98a9", callback: (v) => "$" + Number(v).toLocaleString() }, grid: { color: "#2a3242" } },
        },
      },
    });
  }

  function renderTables(P) {
    $("holdings-table").innerHTML = P.holdings.length
      ? table(
          ["Ticker", "Bucket", "Shares", "Cost Basis", "Price Now", "Value", "Gain/Loss"],
          P.holdings.map((h) => [
            `<b>${esc(h.ticker)}</b>`, esc(h.bucket || "—"), h.shares, money(h.cost_basis_per_share),
            money(h.price_now), money(h.value_now),
            `<span class="${pctClass(h.gain_pct)}">${pct(h.gain_pct)}</span>`,
          ])
        )
      : '<p class="empty">No stock positions yet — everything is in cash. Your first buys will show up here.</p>';

    $("accounts-table").innerHTML = table(
      ["Account", "Cash"],
      (P.accounts || []).map((a) => [esc(a.name), money(a.cash)])
    );

    const paper = P.paper_account;
    $("paper-card").style.display = paper ? "" : "none";
    if (paper) {
      $("paper-table").innerHTML =
        `<p class="muted" style="font-size:12px;margin-top:0">Equity ${money(paper.equity)} · cash ${money(paper.cash)} · as of ${esc(paper.as_of)}</p>` +
        (paper.holdings.length
          ? table(
              ["Ticker", "Shares", "Avg Cost", "Price Now", "Value", "Gain/Loss"],
              paper.holdings.map((h) => [
                `<b>${esc(h.ticker)}</b>`, h.shares, money(h.cost_basis_per_share), money(h.price_now),
                money(h.value_now), `<span class="${pctClass(h.gain_pct)}">${pct(h.gain_pct)}</span>`,
              ])
            )
          : '<p class="empty">No positions in the paper account.</p>');
    }
  }

  renderPortfolio(D.portfolio);

  /* ---------- refresh button (needs dashboard_server.py running) ---------- */
  const refreshBtn = $("refresh-btn");
  const refreshStatus = $("refresh-status");
  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    refreshStatus.className = "refresh-status";
    refreshStatus.textContent = "Refreshing prices…";
    try {
      const res = await fetch("/api/refresh-prices");
      if (!res.ok) throw new Error((await res.json()).error || "Server error");
      const portfolio = await res.json();
      D.portfolio = portfolio;
      renderPortfolio(portfolio);
      refreshStatus.className = "refresh-status ok";
      refreshStatus.textContent = "Updated " + new Date().toLocaleTimeString();
    } catch (err) {
      refreshStatus.className = "refresh-status error";
      // fetch() throws a TypeError when there's no server to talk to at all;
      // anything else is a real error the server reported, and hiding it behind
      // the setup hint just sent you looking in the wrong place.
      refreshStatus.textContent =
        err instanceof TypeError
          ? "Couldn't reach the refresh server — open this page via start_dashboard.bat instead of double-clicking index.html."
          : "Refresh failed: " + err.message;
    } finally {
      refreshBtn.disabled = false;
    }
  });

  /* ---------- news ---------- */
  $("risks-content").innerHTML = mdRender(mdSection("Risks & What to Watch"));
  $("situation-content").innerHTML = mdRender(mdSection("Market Situation"));
  newsList($("news-top"), D.news.top_headlines);
  newsList($("news-market"), D.news.market_news);
  newsList($("news-sector"), D.news.sector_news);

  /* ---------- buy / sell ---------- */
  const catClass = (c) => {
    c = String(c || "").toLowerCase();
    if (c.includes("core")) return "core";
    if (c.includes("growth")) return "growth";
    if (c.includes("defen") || c.includes("income")) return "defensive";
    if (c.includes("spec")) return "speculative";
    if (c.includes("etf") || c.includes("divers")) return "etf";
    return "";
  };
  const label = (s) => String(s || "").replace(/_/g, " ");
  // data.js from before the rebuild has no `advice` block.
  const A = D.advice || { new_signals: [], position_signals: [], stats: null, rules: [] };
  const actionClass = (a) =>
    ["BUY", "BOUGHT"].includes(a) ? "pos"
      : ["SELL", "CANCEL", "NO STOP", "PARTIAL STOP", "RAISE STOP", "STOP EXPIRES"].includes(a) ? "neg"
      : ["TRAILING", "SWITCH TO TRAILING"].includes(a) ? "pos"
      : "muted";

  // Today's new buys/limit buys, anything an open position needs done, and
  // problems found in the brokerage account. Ongoing HOLDs and still-pending
  // limit orders live in the positions table.
  const accountQuiet = ["PROTECTED", "NOT BOUGHT", "UNTRACKED"];
  const actionable = [
    ...(A.new_signals || []).filter((s) => ["BUY", "WAIT"].includes(s.action)),
    ...(A.position_signals || []).filter((s) => !["HOLD", "WAIT"].includes(s.action)),
    ...(A.account_signals || []).filter((s) => !accountQuiet.includes(s.action)).map((s) => ({ ...s, fromAccount: true })),
  ];
  $("action-cards").innerHTML = actionable.length
    ? actionable
        .map((s) => {
          const bits = [];
          if (s.shares && ["BUY", "WAIT"].includes(s.action)) bits.push(`<b>${s.shares} sh</b> (~${money(s.dollars)})`);
          else if (s.size_pct != null && ["BUY", "WAIT"].includes(s.action)) bits.push(`Size <b>${s.size_pct}%</b>`);
          if (s.fromAccount) {
            if (s.qty != null) bits.push(`Held <b>${s.qty}</b>`);
            if (s.stop_order != null) bits.push(`Your stop <b>${money(s.stop_order)}</b>`);
            if (s.plan_stop != null) bits.push(`Plan stop <b>${money(s.plan_stop)}</b>`);
          }
          if (s.buy_limit != null && s.action === "WAIT") bits.push(`Buy ≤ <b>${money(s.buy_limit)}</b>`);
          else if (s.price != null) bits.push(`Price <b>${money(s.price)}</b>`);
          const live = !s.fromAccount && !["closed", "cancelled"].includes(s.status);
          if (s.stop != null && live) bits.push(`Stop <b>${money(s.stop)}</b>`);
          if (s.trail_pct != null && live)
            bits.push(s.stop_phase === "trailing" ? `Trailing <b>${s.trail_pct}%</b>` : `Then trails <b>${s.trail_pct}%</b> once +${s.trail_start_pct}%`);
          if (s.horizon) bits.push(esc({ swing: "Swing (tight trail)", short: "Short-term", short_medium: "Short-term", medium: "Medium", long: "Long trend (wide trail)" }[s.horizon] || s.horizon));
          if (s.return_pct != null) bits.push(`Return <b class="${pctClass(s.return_pct)}">${pct(s.return_pct)}</b>`);
          return `<div class="pick-card">
            <div class="head"><span class="tk">${esc(s.ticker)}</span>
            <span class="cat ${catClass(s.category)}">${esc(label(s.category))}</span></div>
            <div style="font-weight:700" class="${actionClass(s.action)}">${esc(s.action)}</div>
            <div class="muted" style="font-size:12px">${s.fromAccount ? "your Alpaca account" : esc(label(s.setup)) + (s.conviction ? " · conviction " + s.conviction + "/5" : "")}</div>
            <div class="thesis">${esc(s.detail)}</div>
            ${bits.length ? `<div class="fund">${bits.join(" · ")}</div>` : ""}
          </div>`;
        })
        .join("")
    : (() => {
        // Say *why* there's nothing to do: the run's notes (e.g. candidates
        // screened out on price) and the model's own explanation.
        const notes = (A.notes || []).filter((n) => !/PAPER account/.test(n));
        const ideas = mdSection("New Ideas");
        const why = ideas ? ideas.split(/\n\s*\n/)[0] : "";
        // One full-width block: #action-cards is a card grid, so loose
        // children would each land in their own narrow column.
        return '<div style="grid-column:1/-1">' +
          '<p class="empty" style="font-style:normal;color:var(--text);margin:0 0 6px"><b>No buys or sells today.</b></p>' +
          (why ? `<div class="md" style="font-size:13px">${marked.parse(why)}</div>` : "") +
          (notes.length ? `<ul style="font-size:12px;color:var(--muted);padding-left:18px">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "") +
          "</div>";
      })();

  const acct = A.account;
  $("account-card").style.display = acct ? "" : "none";
  if (acct) {
    $("account-title").textContent =
      acct.env === "paper" ? "Your Alpaca Account — PAPER (test money)" : "Your Alpaca Account";
    const perf = A.paper_performance;
    $("autotrade-line").innerHTML = A.autotrade
      ? "<b style='color:var(--accent)'>Auto-trading ON</b> — the agent places these paper trades itself." +
        (perf && perf.return_pct != null
          ? ` Since ${esc(perf.since)} (${perf.days} days): <b class="${pctClass(perf.return_pct)}">${pct(perf.return_pct)}</b>` +
            (perf.spy_return_pct != null ? ` vs S&P 500 <b class="${pctClass(perf.spy_return_pct)}">${pct(perf.spy_return_pct)}</b>` : "")
          : "")
      : "Auto-trading off — you place the orders yourself.";
    const tr = A.trades || [];
    $("trades-block").innerHTML = tr.length
      ? "<h3>Orders the agent placed this run</h3>" + table(
          ["Ticker", "Action", "Result", "Detail"],
          tr.map((t) => [`<b>${esc(t.symbol)}</b>`, esc(label(t.kind)),
            `<span class="${t.ok ? "pos" : "neg"}">${t.ok ? "done" : "failed"}</span>`, esc(t.detail)])
        )
      : "";
    $("account-stats").innerHTML = [
      { k: "Equity", v: money(acct.equity) },
      { k: "Cash", v: money(acct.cash) },
      { k: "Invested", v: money(acct.invested) + (acct.invested_pct != null ? ` (${acct.invested_pct}%)` : "") },
      { k: "Today", v: pct(acct.day_change_pct), cls: pctClass(acct.day_change_pct) },
    ]
      .map((s) => `<div class="stat"><div class="k">${s.k}</div><div class="v ${s.cls || ""}">${s.v}</div></div>`)
      .join("");
    const rows = A.account_signals || [];
    $("account-table").innerHTML = rows.length
      ? table(
          ["Ticker", "Status", "Shares", "Value", "Your stop", "Plan stop", "What to do"],
          rows.map((r) => [
            `<b>${esc(r.ticker)}</b>`, `<span class="${accountQuiet.includes(r.action) ? "muted" : "neg"}">${esc(r.action)}</span>`,
            r.qty ?? "—", money(r.market_value), money(r.stop_order), money(r.plan_stop), esc(r.detail),
          ])
        )
      : '<p class="empty">No positions in the account.</p>';
  }

  const held = (A.position_signals || []).filter((s) => ["HOLD", "WAIT", "RAISE STOP", "TRAILING", "BOUGHT"].includes(s.action));
  $("positions-table").innerHTML = held.length
    ? table(
        ["Ticker", "Status", "Entry", "Now", "Return", "S&P same period", "Stop", "Stop is"],
        held.map((s) => [
          `<b>${esc(s.ticker)}</b>`, s.status === "pending" ? "limit buy" : "holding",
          money(s.entry ?? s.buy_limit), money(s.price),
          `<span class="${pctClass(s.return_pct)}">${pct(s.return_pct)}</span>`,
          `<span class="${pctClass(s.bench_return_pct)}">${pct(s.bench_return_pct)}</span>`,
          money(s.stop),
          esc({ trailing: `trailing ${s.trail_pct}%`, breakeven: "at breakeven", initial: "initial" }[s.stop_phase] || "—"),
        ])
      )
    : '<p class="empty">No open positions yet.</p>';

  const T = A.stats;
  if (T) {
    const c = T.closed || {};
    const agentN = (T.agent_closed || {}).n || 0, legacyN = (T.legacy_closed || {}).n || 0;
    $("track-stats").innerHTML = [
      { k: "Past picks scored", v: c.n || 0, s: `${agentN} from this system · ${legacyN} older, replayed` },
      { k: "Avg return", v: pct(c.avg_return), cls: pctClass(c.avg_return) },
      { k: "Avg vs S&P 500", v: c.avg_excess == null ? "—" : (c.avg_excess > 0 ? "+" : "") + c.avg_excess.toFixed(1) + " pts", cls: pctClass(c.avg_excess) },
      { k: "Beat the S&P", v: c.beat_rate == null ? "—" : c.beat_rate.toFixed(0) + "%" },
    ]
      .map((s) => `<div class="stat"><div class="k">${s.k}</div><div class="v ${s.cls || ""}">${s.v}</div>` +
        (s.s ? `<div class="s muted">${s.s}</div>` : "") + "</div>")
      .join("");
    const recent = T.recent_closed || [];
    $("recent-table").innerHTML = recent.length
      ? table(
          ["Ticker", "Source", "Setup", "Return", "S&P same dates", "vs S&P", "Exit", "Days held"],
          recent.map((r) => [
            `<b>${esc(r.ticker)}</b>`, r.source === "agent" ? "this system" : "older pick (replayed)",
            esc(label(r.setup)), `<span class="${pctClass(r.ret)}">${pct(r.ret)}</span>`, pct(r.bench),
            `<span class="${pctClass(r.excess)}">${r.excess == null ? "—" : (r.excess > 0 ? "+" : "") + r.excess.toFixed(1) + " pts"}</span>`,
            esc(r.reason || ""), r.days,
          ])
        )
      : '<p class="empty">No scored picks yet.</p>';
    const setups = Object.entries(T.by_setup_closed || {}).filter(([, v]) => v.n);
    $("setup-table").innerHTML = setups.length
      ? table(
          ["Setup", "Trades", "Avg return", "vs S&P", "Beat S&P"],
          setups.map(([k, v]) => [
            esc(label(k)), v.n, `<span class="${pctClass(v.avg_return)}">${pct(v.avg_return)}</span>`,
            `<span class="${pctClass(v.avg_excess)}">${v.avg_excess == null ? "—" : (v.avg_excess > 0 ? "+" : "") + v.avg_excess.toFixed(1) + " pts"}</span>`,
            v.beat_rate == null ? "—" : v.beat_rate.toFixed(0) + "%",
          ])
        )
      : '<p class="empty">No closed trades yet — the track record fills in as positions close.</p>';
  } else {
    $("track-stats").innerHTML = "";
    $("setup-table").innerHTML = '<p class="empty">Run the agent once to start the track record.</p>';
  }
  $("rules-list").innerHTML = (A.rules || []).map((r) => `<li>${esc(r)}</li>`).join("") || "<li class='muted'>None yet.</li>";

  $("ideas-content").innerHTML = mdRender(mdSection("New Ideas"));
  $("review-content").innerHTML = mdRender(mdSection("Open Position Review"));

  /* ---------- overall ---------- */
  $("macro-stats").innerHTML = (D.overall.macro || [])
    .map((m) => {
      const isYield = String(m.symbol).includes("TNX");
      const level = isYield ? (m.price / 10).toFixed(2) + "%" : Number(m.price).toLocaleString();
      return `<div class="stat"><div class="k">${esc(m.name)}</div><div class="v">${level}</div>
        <div class="s ${pctClass(m.changePercentage)}">${pct(m.changePercentage)}</div></div>`;
    })
    .join("");

  const sectors = (D.overall.sector_performance || []).slice().sort((a, b) => b.averageChange - a.averageChange);
  const maxAbs = Math.max(0.01, ...sectors.map((s) => Math.abs(s.averageChange)));
  $("sector-bars").innerHTML = sectors.length
    ? sectors
        .map((s) => {
          const w = (Math.abs(s.averageChange) / maxAbs) * 100;
          const color = s.averageChange >= 0 ? "var(--accent)" : "var(--red)";
          return `<div class="sector-bar-row"><span class="name">${esc(s.sector)}</span>
            <span class="bar-zone"><span class="bar" style="width:${w}%;background:${color}"></span></span>
            <span class="val ${pctClass(s.averageChange)}">${pct(s.averageChange)}</span></div>`;
        })
        .join("")
    : '<p class="empty">No sector data (markets may be closed).</p>';

  const moverRows = (rows) =>
    (rows || []).map((r) => [
      `<b>${esc(r.symbol)}</b>`, esc(r.name), money(r.price),
      `<span class="${pctClass(r.changesPercentage)}">${pct(r.changesPercentage)}</span>`,
      r.marketCap ? mcap(r.marketCap) : "—",
    ]);
  const moverHead = ["Symbol", "Name", "Price", "Change", "MCap"];
  $("gainers-table").innerHTML = table(moverHead, moverRows(D.overall.gainers));
  $("losers-table").innerHTML = table(moverHead, moverRows(D.overall.losers));
  $("active-table").innerHTML = table(moverHead, moverRows(D.overall.most_active));

  $("earnings-table").innerHTML = table(
    ["Symbol", "Date", "Est. EPS", "Est. Revenue"],
    (D.overall.earnings_calendar || []).slice(0, 20).map((e) => [
      `<b>${esc(e.symbol)}</b>`, esc(e.date), e.epsEstimated ?? "—",
      e.revenueEstimated ? mcap(e.revenueEstimated) : "—",
    ])
  );

  /* ---------- full report ---------- */
  $("full-report").innerHTML = marked.parse(D.report_markdown || "");
})();
