/* AI usage dashboard - vanilla JS, no dependencies. Reads cached state from the Pi; never calls providers. */
(function () {
  "use strict";

  // Colour follows the platform (fixed slot per entity), never its rank.
  var SLOT = { claude: 1, codex: 2, antigravity: 3, gemini: 4, opencode: 5, openrouter: 6, moonshot: 7, kimi_code: 8 };
  var DARK_INK_SLOTS = { 4: true }; // yellow needs dark text inside a fill (light mode)
  var REFRESH_MS = 30000;

  var S = { state: null, settings: null, month: null, credits: "used", trendTable: false, open: {}, view: "dash" };
  var N = { data: null, filter: "you", valueTable: false, index: null };
  var $ = function (sel, el) { return (el || document).querySelector(sel); };

  // ================================================================== helpers
  function color(pid) { return SLOT[pid] ? "var(--s" + SLOT[pid] + ")" : "var(--other)"; }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function load(k, d) { try { var v = localStorage.getItem("aidash." + k); return v == null ? d : v; } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem("aidash." + k, v); } catch (e) { /* private mode */ } }
  function money(v, cur) {
    if (v == null || isNaN(v)) return "—";
    cur = cur || (S.state && S.state.currency) || "USD";
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2 }).format(v);
    } catch (e) { return v.toFixed(2) + " " + cur; }
  }
  function usd(v) {
    if (v == null) return "—";
    if (v === 0) return "free";
    return "$" + (v < 0.1 ? v.toFixed(3) : v < 10 ? v.toFixed(2) : v.toFixed(0));
  }
  function pct(v) { return v == null ? "—" : (v >= 10 || v === 0 ? Math.round(v) : v.toFixed(1)) + "%"; }
  function ago(ts) {
    if (!ts) return "never";
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  function until(ts) {
    if (!ts) return "";
    var s = ts - Date.now() / 1000;
    if (s <= 0) return "resetting";
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return "resets in " + d + "d " + h + "h";
    if (h) return "resets in " + h + "h " + m + "m";
    return "resets in " + m + "m";
  }
  function day(ts) { return ts ? new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short" }) : ""; }
  function monthLabel(m) { var p = m.split("-"); return new Date(+p[0], +p[1] - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" }); }
  function shortMonth(m) { var p = m.split("-"); return new Date(+p[0], +p[1] - 1, 1).toLocaleDateString(undefined, { month: "short" }); }
  function star(m) { return m && m.watched ? '<span class="you" title="A family you watch">★</span>' : ""; }
  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.hidden = true; }, 3500);
  }
  function api(path, body) {
    var opts = { headers: {} };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
      var pw = null; try { pw = sessionStorage.getItem("aidash.admin"); } catch (e) { /* ignore */ }
      if (pw) opts.headers["X-Admin-Password"] = pw;
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.status === 401 && body !== undefined) {
          var p = window.prompt("Admin password for this dashboard:");
          if (p) { try { sessionStorage.setItem("aidash.admin", p); } catch (e) { /* ignore */ } return api(path, body); }
        }
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }

  // ================================================================== tooltip (hover + tap)
  var tip = null, tipOwner = null;
  function showTip(html, x, y) {
    tip = tip || $("#tooltip");
    tip.innerHTML = html; tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight, px = x + 14, py = y + 14;
    if (px + w > window.innerWidth - 8) px = x - w - 14;
    if (py + h > window.innerHeight - 8) py = y - h - 14;
    tip.style.left = Math.max(8, px) + "px"; tip.style.top = Math.max(8, py) + "px";
  }
  function hideTip() { if (tip) tip.hidden = true; tipOwner = null; }
  function bindTips(root) {
    root.querySelectorAll("[data-tip]").forEach(function (el) {
      el.addEventListener("pointermove", function (e) { if (e.pointerType === "mouse") showTip(el.getAttribute("data-tip"), e.clientX, e.clientY); });
      el.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse") hideTip(); });
      el.addEventListener("click", function (e) {  // tap on phones (no hover)
        e.stopPropagation();
        if (tipOwner === el) { hideTip(); return; }
        var r = el.getBoundingClientRect();
        showTip(el.getAttribute("data-tip"), r.left + Math.min(r.width / 2, 120), r.bottom - 6);
        tipOwner = el;
      });
      el.addEventListener("focus", function () {
        var r = el.getBoundingClientRect();
        showTip(el.getAttribute("data-tip"), r.left + r.width / 2, r.bottom);
      });
      el.addEventListener("blur", hideTip);
    });
  }
  document.addEventListener("click", function () { if (tipOwner) hideTip(); });
  window.addEventListener("scroll", function () { if (tipOwner) hideTip(); }, { passive: true });

  // ================================================================== featured headline
  var URGENT = { alert: 1, stealth: 1, retiring: 1 };
  function featuredHtml(f, large) {
    if (!f) return '<div class="empty-note">Loading headline…</div>';
    var external = f.url && f.url.charAt(0) !== "#";
    var titleTag = f.url ? '<a class="f-title" href="' + esc(f.url) + '"' + (external ? ' target="_blank" rel="noopener noreferrer"' : "") + ">" : '<span class="f-title">';
    var meta = [];
    if (f.reason) meta.push("<span>" + esc(f.reason) + "</span>");
    if (f.source) meta.push("<span>" + esc(f.source) + "</span>");
    if (f.ts && f.kind !== "retiring" && f.kind !== "stealth" && f.kind !== "new_model") meta.push("<span>" + day(f.ts) + "</span>");
    var actions = "";
    if (f.url) actions += '<a class="btn ' + (large ? "primary" : "") + ' small" href="' + esc(f.url) + '"' + (external ? ' target="_blank" rel="noopener noreferrer"' : "") + ">" +
      (external ? "Read" : "View in Models") + "</a>";
    if (f.discuss && f.discuss !== f.url) actions += '<a class="btn ghost small" href="' + esc(f.discuss) + '" target="_blank" rel="noopener noreferrer">Discussion</a>';
    if (!large) actions += '<a class="btn ghost small" href="#/news">More news</a>';
    return '<span class="f-tag' + (URGENT[f.kind] ? " urgent" : "") + '">' + esc(f.tag || "News") + "</span>" +
      titleTag + esc(f.title) + (f.url ? "</a>" : "</span>") +
      (f.summary ? '<p class="f-sum">' + esc(f.summary) + "</p>" : "") +
      (meta.length ? '<div class="f-meta">' + meta.join("") + "</div>" : "") +
      (actions ? '<div class="f-actions">' + actions + "</div>" : "");
  }

  // ================================================================== overview
  function refresh() {
    var main = $("#view-dash");
    if (S.state) main.classList.add("loading"); // hold previous render, no skeleton flash
    var q = S.month ? "?month=" + encodeURIComponent(S.month) : "";
    return api("/api/state" + q).then(function (st) {
      S.state = st;
      if (!S.month) S.month = st.current_month;
      render();
    }).catch(function (e) {
      $("#updated").textContent = "Pi unreachable: " + e.message;
    }).then(function () { main.classList.remove("loading"); });
  }

  function render() {
    var st = S.state;
    renderFilters(st);
    $("#featured").innerHTML = featuredHtml(st.highlights && st.highlights.featured, false);
    renderHero(st);
    renderSpotlight(st);
    renderBreakdown(st);
    renderHeadlines(st);
    renderActive(st);
    renderTrend(st);
    renderCards(st);
    var fx = st.fx || {};
    $("#fx-note").textContent = "Exchange rates: " + (fx.source || "?") + (fx.day ? " (" + fx.day + ")" : "") +
      (fx.error ? " — last refresh failed: " + fx.error : "") + ". Amounts converted to " + st.currency + ". v" + st.version + ".";
    $("#updated").textContent = "Updated " + new Date(st.now * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function renderFilters(st) {
    var fm = $("#f-month");
    if (fm.options.length !== st.months.length) {
      fm.innerHTML = st.months.map(function (m) { return '<option value="' + m + '">' + esc(monthLabel(m)) + "</option>"; }).join("");
    }
    fm.value = st.month;
    var fc = $("#f-currency");
    if (fc.options.length !== st.currencies.length) {
      fc.innerHTML = st.currencies.map(function (c) { return '<option value="' + c + '">' + c + "</option>"; }).join("");
    }
    fc.value = st.currency;
    $("#f-credits").value = S.credits;
  }

  function rowTotal(r) { return S.credits === "bought" ? r.subscription + r.topups : r.total; }

  function renderHero(st) {
    var t = st.costs.totals, bought = S.credits === "bought";
    $("#hero-month").textContent = monthLabel(st.month);
    $("#hero-total").textContent = money(bought ? t.total_cashflow : t.total);
    $("#hero-sub").textContent = bought ? "Subscriptions + credits bought this month (cash out)." : "Subscriptions + credit actually spent this month, all platforms.";
    $("#t-subs").textContent = money(t.subscriptions);
    $("#t-spend-label").textContent = bought ? "Credits bought" : "Credit spend";
    $("#t-spend").textContent = money(bought ? t.topups : t.credit_spend);
    $("#t-topups-label").innerHTML = (bought ? "Credit spend" : "Credits bought") + ' <span class="muted">(not in total)</span>';
    $("#t-topups").textContent = money(bought ? t.credit_spend : t.topups);
    var bal = 0, any = false;
    st.costs.rows.forEach(function (r) { if (r.balance_left != null) { bal += r.balance_left; any = true; } });
    $("#t-balance").textContent = any ? money(bal) : "—";
    var CREDIT = { openrouter: "OpenRouter", moonshot: "Moonshot (Kimi API)" };
    var missing = st.platforms.filter(function (p) { return CREDIT[p.id] && p.status === "unconfigured"; }).map(function (p) { return CREDIT[p.id]; });
    var msg = "";
    if (missing.length) msg = "Credit figures come from your prepaid platforms — " + esc(missing.join(" and ")) + (missing.length > 1 ? " aren't" : " isn't") +
      ' connected yet. <a href="#/settings">Add the key' + (missing.length > 1 ? "s" : "") + " →</a> ";
    if (!t.topups && st.month === st.current_month) msg += "Purchases are detected when a balance goes up while the dashboard is watching; earlier ones aren't visible to any API — " +
      '<a href="#/settings">add them as invoices</a>.';
    $("#credit-note").innerHTML = msg;
    $("#credit-note").hidden = !msg;
  }

  function renderSpotlight(st) {
    var h = st.highlights, el = $("#spotlight");
    if (!h) { el.innerHTML = ""; return; }
    var sp = h.spotlight || {}, cards = [];
    (sp.alerts || []).slice(0, 1).forEach(function (a) {
      cards.push('<a class="spot alert" href="#/models"><span class="k">⚠ Alert</span><span class="v">' + esc(a.title) + '</span><span class="d">' + esc(a.detail || "") + "</span></a>");
    });
    if (sp.new_model) cards.push('<a class="spot" href="#/models"><span class="k">★ New model</span><span class="v">' + esc(sp.new_model.name) +
      '</span><span class="d">' + day(sp.new_model.created) + " · " + usd(sp.new_model.blended) + "/M" + (sp.new_model.q ? " · quality " + sp.new_model.q : "") + "</span></a>");
    if (sp.best_value) cards.push('<a class="spot" href="#/models"><span class="k">Best value under $0.50/M</span><span class="v">' + esc(sp.best_value.name) +
      '</span><span class="d">' + usd(sp.best_value.blended) + "/M · quality " + sp.best_value.q + "</span></a>");
    cards.push('<a class="spot" href="#/models"><span class="k">Stealth models</span><span class="v">' +
      (sp.stealth && sp.stealth.length ? sp.stealth.length + " live" : "None right now") + '</span><span class="d">' +
      (sp.stealth && sp.stealth.length ? esc(sp.stealth.map(function (s) { return s.name; }).join(", ")) : "unnamed models under test on OpenRouter") + "</span></a>");
    if (cards.length < 4) cards.push('<a class="spot" href="#/models"><span class="k">Tracked models</span><span class="v">' + (sp.tracked_count || 0) +
      " pinned</span><span class=\"d\">" + (sp.tracked_count ? "price, retirement &amp; new-version alerts" : "pin models to follow their price") + "</span></a>");
    el.innerHTML = cards.slice(0, 4).join("");
  }

  function renderBreakdown(st) {
    var rows = st.costs.rows.slice().sort(function (a, b) { return rowTotal(b) - rowTotal(a) || a.name.localeCompare(b.name); });
    var grand = rows.reduce(function (s, r) { return s + rowTotal(r); }, 0);
    var bar = $("#segbar");
    var nonzero = rows.filter(function (r) { return rowTotal(r) > 0; });
    if (!grand) {
      bar.className = "segbar empty"; bar.textContent = "No costs yet this month — add subscription prices in Settings.";
      bar.setAttribute("aria-label", "No costs recorded");
    } else {
      bar.className = "segbar";
      var segs = nonzero.slice(0, 6);
      if (nonzero.length > 6) {
        var rest = nonzero.slice(5);
        segs = nonzero.slice(0, 5).concat([{ platform: "_other", name: "Other (" + rest.length + ")", _v: rest.reduce(function (s, r) { return s + rowTotal(r); }, 0) }]);
      }
      bar.innerHTML = segs.map(function (r) {
        var v = r._v != null ? r._v : rowTotal(r), share = 100 * v / grand;
        var dark = DARK_INK_SLOTS[SLOT[r.platform]] ? " dark-ink" : "";
        return '<div class="seg' + dark + '" tabindex="0" style="flex:' + v + ' 1 0;background:' + color(r.platform) + '" data-tip="<b>' +
          esc(r.name) + "</b><br>" + esc(money(v)) + " · " + share.toFixed(1) + '%"><span>' + (share >= 12 ? esc(r.name) + " " + Math.round(share) + "%" : "") + "</span></div>";
      }).join("");
      bar.setAttribute("aria-label", "Share of monthly total: " + segs.map(function (r) {
        var v = r._v != null ? r._v : rowTotal(r); return r.name + " " + Math.round(100 * v / grand) + "%";
      }).join(", "));
      bindTips(bar);
    }
    $("#bd-note").textContent = S.credits === "bought" ? "Total = subscription + credits bought" : "Total = subscription + credit spend";
    var dash = '<span class="muted">—</span>';
    var tb = $("#bd-table tbody");
    tb.innerHTML = rows.map(function (r) {
      var open = !!S.open[r.platform];
      var usage = r.usage_pct == null ? dash
        : '<span class="mini-meter" aria-hidden="true"><i style="width:' + Math.min(100, r.usage_pct) + '%"></i></span>' + pct(r.usage_pct) +
          ' <span class="muted small">' + esc(r.usage_label || "") + "</span>";
      var share = grand ? (100 * rowTotal(r) / grand).toFixed(1) + "%" : "—";
      var html = '<tr class="expander" tabindex="0" data-p="' + esc(r.platform) + '" aria-expanded="' + open + '">' +
        '<td class="c-name"><span class="chev">' + (r.items.length ? (open ? "▾" : "▸") : "") + '</span><span class="swatch" style="background:' + color(r.platform) + '"></span>' + esc(r.name) + "</td>" +
        '<td class="num" data-label="Subscription">' + (r.subscription ? money(r.subscription) : dash) + "</td>" +
        '<td class="num" data-label="Credit spend">' + (r.credit_spend ? money(r.credit_spend) : dash) + "</td>" +
        '<td class="num" data-label="Total"><b>' + money(rowTotal(r)) + "</b></td>" +
        '<td class="num c-hide-sm" data-label="Share">' + share + "</td>" +
        '<td class="num' + (r.topups ? "" : " c-hide-sm") + '" data-label="Bought">' + (r.topups ? money(r.topups) : dash) + "</td>" +
        '<td class="num' + (r.balance_left != null ? "" : " c-hide-sm") + '" data-label="Credit left">' + (r.balance_left != null ? money(r.balance_left) : dash) + "</td>" +
        '<td class="c-usage' + (r.usage_pct == null ? " c-hide-sm" : "") + '" data-label="Usage now">' + usage + "</td></tr>";
      if (open && r.items.length) {
        html += '<tr class="detail"><td colspan="8"><ul class="detail-list">' + r.items.map(function (it) {
          var kind = it.kind === "topup" ? "bought" : (it.kind === "info" ? "replaced" : it.kind);
          return "<li><span>" + esc(it.label) + ' <span class="muted">(' + esc(kind) + (it.original ? ", " + esc(it.original) : "") +
            ')</span></span><span class="num">' + money(it.amount) + "</span></li>";
        }).join("") + "</ul></td></tr>";
      }
      return html;
    }).join("");
    var t = st.costs.totals;
    var withUse = rows.filter(function (r) { return r.usage_pct != null; });
    var tight = withUse.slice().sort(function (a, b) { return b.usage_pct - a.usage_pct; })[0];
    var hot = withUse.filter(function (r) { return r.usage_pct >= 80; }).length;
    var usageSummary = tight
      ? '<span class="mini-meter" aria-hidden="true"><i style="width:' + Math.min(100, tight.usage_pct) + '%"></i></span>Tightest ' + pct(tight.usage_pct) +
        ' <span class="muted small">' + esc(tight.name) + " · " + esc(tight.usage_label || "") + "</span><br>" +
        '<span class="small">' + (hot ? '<span class="warnline">⚠ ' + hot + " of " + withUse.length + " platform" + (withUse.length > 1 ? "s" : "") + " ≥ 80%</span>"
          : (withUse.length > 1 ? "all " + withUse.length + " platforms below 80%" : "below 80%")) + "</span>"
      : dash;
    $("#bd-table tfoot").innerHTML = '<tr><td class="c-name">All platforms</td>' +
      '<td class="num" data-label="Subscriptions">' + money(t.subscriptions) + '</td><td class="num" data-label="Credit spend">' + money(t.credit_spend) + "</td>" +
      '<td class="num" data-label="Total">' + money(S.credits === "bought" ? t.total_cashflow : t.total) + '</td><td class="num c-hide-sm" data-label="Share">100%</td>' +
      '<td class="num" data-label="Bought">' + money(t.topups) + '</td><td class="num c-hide-sm" data-label="Credit left">' + $("#t-balance").textContent + '</td><td class="c-usage" data-label="Usage">' + usageSummary + "</td></tr>";
    tb.querySelectorAll("tr.expander").forEach(function (tr) {
      var toggle = function () { var p = tr.getAttribute("data-p"); S.open[p] = !S.open[p]; renderBreakdown(S.state); };
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
  }

  function newsItemHtml(i, compact) {
    var GROUP = { release: "release", vendor: "news", community: "community", stealth: "stealth" };
    var src = '<span class="tag">' + esc(GROUP[i.group] || i.group || "news") + "</span><span>" + esc(i.source || "") + "</span><span>" + day(i.ts) + "</span>" +
      (i.points ? "<span>" + i.points + " pts</span>" : "") +
      (i.discuss && i.discuss !== i.url ? '<a href="' + esc(i.discuss) + '" target="_blank" rel="noopener noreferrer">discussion</a>' : "");
    return '<a class="t" href="' + esc(i.url) + '" target="_blank" rel="noopener noreferrer">' + esc(i.title) + '</a><div class="src">' + src + "</div>" +
      (!compact && i.summary ? '<div class="sum">' + esc(i.summary) + "</div>" : "");
  }

  function renderHeadlines(st) {
    var h = st.highlights, el = $("#headlines");
    var list = (h && h.headlines) || [];
    el.innerHTML = list.length ? list.map(function (i) { return '<div class="hl-item">' + newsItemHtml(i, true) + "</div>"; }).join("")
      : '<div class="empty-note">The first news fetch runs a minute after start-up…</div>';
  }

  function renderActive(st) {
    var el = $("#active"), now = Date.now() / 1000;
    var list = st.active_windows.filter(function (a) { return a.resets_at > now; });
    if (!list.length) {
      el.innerHTML = '<div class="empty-note">No 5-hour windows reported yet. They appear once Claude, ChatGPT, OpenCode, Kimi Code or Antigravity are connected.</div>';
      return;
    }
    el.innerHTML = '<div class="aw">' + list.map(function (a) {
      var timeFrac = Math.min(100, Math.max(0, 100 * (now - a.started_at) / a.window_s));
      return '<div class="aw-row" style="--pc:' + color(a.platform) + '">' +
        '<div class="aw-line"><span><span class="swatch" style="background:' + color(a.platform) + '"></span><b>' + esc(a.name) + "</b> · " + esc(a.label) + "</span>" +
        '<span class="num">' + (a.used_pct === 0 ? '<span class="muted">idle</span>' : pct(a.used_pct) + " used") + "</span></div>" +
        '<div class="aw-track" tabindex="0" data-tip="Started ' + esc(new Date(a.started_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })) +
        "<br>" + esc(until(a.resets_at)) + "<br>Grey = time elapsed, colour = quota used" + '">' +
        '<div class="aw-time" style="width:' + timeFrac + '%"></div><div class="aw-use" style="width:' + Math.min(100, a.used_pct) + '%"></div></div>' +
        '<div class="meter-foot"><span>' + Math.round(timeFrac) + "% of window elapsed</span><span>" + esc(until(a.resets_at)) + "</span></div></div>";
    }).join("") + "</div>";
    bindTips(el);
  }

  function renderTrend(st) {
    var el = $("#trend"), data = st.trend, btn = $("#tr-toggle");
    btn.textContent = S.trendTable ? "Chart" : "Table";
    btn.setAttribute("aria-pressed", String(S.trendTable));
    var val = function (d) { return S.credits === "bought" ? d.subscriptions + d.topups : d.total; };
    if (S.trendTable) {
      el.innerHTML = '<div class="table-scroll"><table><thead><tr><th>Month</th><th class="num">Subscriptions</th><th class="num">' +
        (S.credits === "bought" ? "Bought" : "Credit spend") + '</th><th class="num">Total</th></tr></thead><tbody>' +
        data.map(function (d) {
          return "<tr><td>" + esc(monthLabel(d.month)) + '</td><td class="num">' + money(d.subscriptions) + '</td><td class="num">' +
            money(S.credits === "bought" ? d.topups : d.credit_spend) + '</td><td class="num">' + money(val(d)) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
      return;
    }
    var W = 560, H = 190, padL = 56, padB = 24, padT = 16, n = data.length;
    var max = Math.max.apply(null, data.map(val).concat([1]));
    var step = Math.pow(10, Math.floor(Math.log10(max))), nice = Math.ceil(max / step) * step;
    var plotW = W - padL - 8, plotH = H - padB - padT, bw = Math.min(56, plotW / n - 14);
    var y = function (v) { return padT + plotH - (v / nice) * plotH; };
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Monthly totals for the last six months">';
    [0, 0.5, 1].forEach(function (f) {
      var gy = y(nice * f);
      svg += '<line x1="' + padL + '" x2="' + (W - 8) + '" y1="' + gy + '" y2="' + gy + '" stroke="var(--' + (f === 0 ? "axis" : "grid") + ')" stroke-width="1"/>';
      svg += '<text x="' + (padL - 6) + '" y="' + (gy + 4) + '" text-anchor="end" font-size="11" fill="var(--muted)" style="font-variant-numeric:tabular-nums">' + esc(money(nice * f)) + "</text>";
    });
    data.forEach(function (d, i) {
      var cx = padL + (i + 0.5) * (plotW / n), v = val(d), top = y(v), h = Math.max(0, y(0) - top);
      var current = d.month === st.current_month, r = Math.min(4, h);
      var path = h > 0 ? "M" + (cx - bw / 2) + "," + y(0) + " V" + (top + r) + " Q" + (cx - bw / 2) + "," + top + " " + (cx - bw / 2 + r) + "," + top +
        " H" + (cx + bw / 2 - r) + " Q" + (cx + bw / 2) + "," + top + " " + (cx + bw / 2) + "," + (top + r) + " V" + y(0) + " Z" : "";
      var tipHtml = "<b>" + esc(monthLabel(d.month)) + "</b><br>Subscriptions " + esc(money(d.subscriptions)) + "<br>" +
        (S.credits === "bought" ? "Bought " + esc(money(d.topups)) : "Credit spend " + esc(money(d.credit_spend))) + "<br><b>Total " + esc(money(v)) + "</b>";
      svg += '<g tabindex="0" data-tip="' + tipHtml.replace(/"/g, "&quot;") + '">' +
        '<rect x="' + (cx - plotW / n / 2) + '" y="' + padT + '" width="' + (plotW / n) + '" height="' + plotH + '" fill="transparent"/>' +
        (path ? '<path d="' + path + '" fill="var(--' + (current ? "ink" : "axis") + ')"/>' : "") + "</g>";
      svg += '<text x="' + cx + '" y="' + (H - 6) + '" text-anchor="middle" font-size="11" fill="var(--' + (current ? "ink" : "muted") + ')">' + esc(shortMonth(d.month)) + "</text>";
      if (current && v > 0) svg += '<text x="' + cx + '" y="' + (top - 5) + '" text-anchor="middle" font-size="11" font-weight="600" fill="var(--ink)">' + esc(money(v)) + "</text>";
    });
    el.innerHTML = svg + "</svg>";
    bindTips(el);
  }

  var STATUS = {
    ok: ["ok", "Live"], stale: ["stale", "Stale"], error: ["error", "Error"], relogin: ["relogin", "Login expired"],
    unconfigured: ["unconfigured", "Not set up"], pending: ["pending", "Starting…"], disabled: ["disabled", "Disabled"]
  };

  function sparkline(pid, series, windowKey) {
    if (!series || series.length < 2) return "";
    var W = 300, H = 44, t0 = series[0][0], t1 = series[series.length - 1][0], span = Math.max(1, t1 - t0);
    var d = "M" + series.map(function (p) { return ((p[0] - t0) / span * (W - 4) + 2).toFixed(1) + "," + (H - 4 - (p[1] / 100) * (H - 8)).toFixed(1); }).join(" L");
    return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="' + esc(windowKey) + ' usage, last 48 hours" data-pid="' + pid + '" data-key="' + esc(windowKey) + '">' +
      '<line x1="0" x2="' + W + '" y1="' + (H - 4) + '" y2="' + (H - 4) + '" stroke="var(--axis)" stroke-width="1"/>' +
      '<path d="' + d + '" fill="none" stroke="' + color(pid) + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>' +
      '<line class="xh" x1="0" x2="0" y1="0" y2="' + H + '" stroke="var(--muted)" stroke-width="1" vector-effect="non-scaling-stroke" visibility="hidden"/></svg>';
  }

  function bindSparks(root) {
    root.querySelectorAll("svg.spark").forEach(function (svg) {
      var pid = svg.getAttribute("data-pid"), key = svg.getAttribute("data-key");
      var p = S.state.platforms.filter(function (x) { return x.id === pid; })[0];
      var series = (p && p.sparks[key]) || [], xh = svg.querySelector(".xh");
      var at = function (e) {  // pointer events: mouse, pen and touch alike
        var r = svg.getBoundingClientRect(), fx = (e.clientX - r.left) / r.width;
        var t0 = series[0][0], t1 = series[series.length - 1][0], t = t0 + fx * (t1 - t0), best = series[0];
        series.forEach(function (s) { if (Math.abs(s[0] - t) < Math.abs(best[0] - t)) best = s; });
        var x = (best[0] - t0) / Math.max(1, t1 - t0) * 296 + 2;
        xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
        showTip("<b>" + pct(best[1]) + " used</b><br>" + esc(new Date(best[0] * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })), e.clientX, e.clientY);
      };
      svg.addEventListener("pointermove", at);
      svg.addEventListener("pointerdown", at);
      svg.addEventListener("pointerleave", function () { xh.setAttribute("visibility", "hidden"); hideTip(); });
    });
  }

  function renderCards(st) {
    var el = $("#cards");
    el.innerHTML = st.platforms.map(function (p) {
      var stt = STATUS[p.status] || ["pending", p.status || "…"];
      var sub = [p.plan, p.account].filter(Boolean).map(esc).join(" · ");
      var html = '<article class="card pcard" style="--pc:' + color(p.id) + '" aria-labelledby="pc-' + p.id + '">' +
        '<div class="pcard-head"><div><div class="pcard-title" id="pc-' + p.id + '">' + esc(p.name) + "</div>" +
        '<div class="pcard-sub">' + (sub || '<span class="muted">' + (p.auth === "oauth" ? "OAuth login" : "API key") + "</span>") + "</div></div>" +
        '<span class="badge ' + stt[0] + '"><span class="dot" aria-hidden="true"></span>' + stt[1] + "</span></div>";
      if (p.error && p.status !== "ok") {
        html += '<div class="err">' + (p.status === "unconfigured" ? "" : "⚠ ") + esc(p.error) +
          (p.status === "unconfigured" || p.status === "relogin" ? ' <a href="#/settings">Settings →</a>' : "") + "</div>";
      }
      var primary = null;
      p.windows.forEach(function (w) {
        if (w.used_pct == null) return;
        if (!primary || (w.window_s && w.window_s >= 6 * 86400 && !(primary.window_s >= 6 * 86400))) primary = w;
        var hot = w.used_pct >= 90, proj = w.projected_pct;
        var paceTxt = proj != null && proj > 100 && w.used_pct < 100 ? '<span class="warnline">▲ on pace for ' + Math.round(proj) + "%</span>" : "";
        html += '<div class="meter"><div class="meter-top"><span>' + esc(w.label) + "</span><b>" + (hot ? '<span class="warnline">⚠ </span>' : "") + pct(w.used_pct) + "</b></div>" +
          '<div class="meter-bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + w.used_pct + '" aria-label="' + esc(w.label) + '">' +
          '<div class="fill" style="width:' + Math.min(100, w.used_pct) + '%"></div>' +
          (proj != null && w.window_s ? '<div class="pace" style="left:calc(' + Math.min(100, Math.max(0, 100 * (Date.now() / 1000 - w.started_at) / w.window_s)) + '% - 1px)" title="Time elapsed in this window"></div>' : "") +
          "</div>" + '<div class="meter-foot"><span>' + esc(until(w.resets_at)) + "</span>" + paceTxt + "</div></div>";
      });
      if (p.balances.length) {
        html += '<div class="balances">' + p.balances.map(function (b) {
          var v = b.currency ? money(b.amount, b.currency) : (Math.round(b.amount * 100) / 100).toLocaleString();
          return "<div>" + esc(b.label) + "<b>" + esc(v) + "</b></div>";
        }).join("") + "</div>";
      }
      var m = p.metrics || {};
      if (p.id === "openrouter" && m.key_usage_month != null) {
        html += '<div class="balances"><div>Today<b>' + money(m.key_usage_day, "USD") + "</b></div><div>This week<b>" + money(m.key_usage_week, "USD") +
          "</b></div><div>This month<b>" + money(m.key_usage_month, "USD") + "</b></div></div>";
      }
      if (p.id === "claude" && m.extra_usage_amount != null) {
        var xc = m.extra_usage_currency || "USD";
        html += '<div class="balances"><div>Extra usage (this cycle)<b>' + money(m.extra_usage_amount, xc) + "</b></div>" +
          (m.extra_usage_limit ? "<div>Cap<b>" + money(m.extra_usage_limit, xc) + "</b></div>" : "") + "</div>";
      }
      if (primary && p.sparks[primary.key]) html += sparkline(p.id, p.sparks[primary.key], primary.key);
      if (p.notes.length) html += '<ul class="notes">' + p.notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") + "</ul>";
      html += '<div class="pcard-foot"><span>' + (p.updated_at ? "Data " + ago(p.updated_at) : "No data yet") + (p.status === "stale" ? " (last good)" : "") + "</span>" +
        (p.status !== "unconfigured" && p.status !== "disabled" ? '<button class="btn ghost small" data-refresh="' + p.id + '">Refresh</button>' : "") + "</div></article>";
      return html;
    }).join("");
    el.querySelectorAll("[data-refresh]").forEach(function (b) {
      b.addEventListener("click", function () {
        b.disabled = true;
        api("/api/refresh/" + b.getAttribute("data-refresh"), {}).then(function (r) {
          var wait = Math.max(0, Math.round(r.next_poll - Date.now() / 1000));
          toast(wait > 3 ? "Refresh queued (in " + wait + "s — rate-limit guard)" : "Refreshing…");
          setTimeout(refresh, (wait + 6) * 1000);
        }).catch(function (e) { toast(e.message); });
      });
    });
    bindSparks(el);
  }

  // ================================================================== news page
  var FILTERS = [["you", "For you"], ["release", "Releases"], ["vendor", "Vendors"], ["community", "Community"], ["stealth", "Stealth chatter"], ["all", "All"]];

  function loadNewsData() {
    return api("/api/news").then(function (d) { markNewestReleases(d.items); N.data = d; return d; });
  }

  function renderNews() {
    var d = N.data;
    var up = Math.max(d.updated.feeds || 0, d.updated.catalog || 0);
    $("#news-updated").textContent = up ? "updated " + ago(up) : "first fetch runs a minute after start-up…";
    $("#news-featured").innerHTML = featuredHtml(d.featured, true);
    $("#n-filters").innerHTML = FILTERS.map(function (f) {
      var n = d.items.filter(function (i) { return matchFilter(i, f[0]); }).length;
      return '<button class="chip" data-f="' + f[0] + '" aria-pressed="' + (N.filter === f[0]) + '">' + f[1] + '<span class="n">' + n + "</span></button>";
    }).join("");
    $("#n-filters").querySelectorAll(".chip").forEach(function (b) {
      b.addEventListener("click", function () { N.filter = b.getAttribute("data-f"); save("newsFilter", N.filter); renderNews(); });
    });
    var items = d.items.filter(function (i) { return matchFilter(i, N.filter) && i.url !== d.featured.url; }).slice(0, 60);
    $("#n-feed").innerHTML = items.length ? items.map(function (i) { return '<article class="news-card">' + newsItemHtml(i, false) + "</article>"; }).join("")
      : '<div class="empty-note">Nothing here yet.</div>';
    var errs = Object.keys(d.errors || {});
    $("#news-errors").textContent = errs.length ? "Some sources failed last time: " + errs.map(function (k) { return k + " (" + d.errors[k] + ")"; }).join("; ") : "";
  }
  function matchFilter(i, f) {
    if (f === "all") return true;
    if (f === "you") return i.relevant && i.group !== "stealth" && !i.prerelease && (i.group !== "release" || i._newest);
    return i.group === f;
  }
  // "For you" shows only the newest release per tool, so release notes don't drown the news.
  function markNewestReleases(items) {
    var seen = {};
    items.forEach(function (i) {
      if (i.group !== "release") return;
      i._newest = !seen[i.source];
      seen[i.source] = true;
    });
  }

  // ================================================================== models page
  function renderModels() {
    var d = N.data, m = d.models || {};
    $("#models-updated").textContent = d.updated.catalog ? "catalogue updated " + ago(d.updated.catalog) + " · " + (m.catalog_size || 0) + " models" : "";
    $("#m-alerts").innerHTML = (d.alerts || []).length ? '<div class="alert-strip">' + d.alerts.slice(0, 6).map(function (a) {
      return '<div class="alert-row sev' + a.severity + '"><span class="ico" aria-hidden="true">' + (a.severity >= 3 ? "⚠" : "•") + "</span><span><b>" + esc(a.title) + '</b> <span class="muted small">' + esc(a.detail || "") + "</span></span></div>";
    }).join("") + "</div>" : "";
    renderTracked(m.tracked || []);

    var st = m.stealth || [], gone = m.stealth_gone || [];
    var chatter = d.items.filter(function (i) { return i.group === "stealth"; }).slice(0, 4);
    var h = st.length ? '<ul class="mlist">' + st.map(function (x) {
      return "<li><div><b>" + esc(x.name) + '</b><div class="mid">' + esc(x.id) + " · " + esc(x.desc || "") + '</div></div><div class="meta">' +
        usd(x.blended) + (x.ctx ? "<br>" + Math.round(x.ctx / 1000) + "k ctx" : "") + "<br>since " + day(x.first_seen) + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">No stealth models live on OpenRouter right now — this lights up (and takes the headline) when one appears.</div>';
    if (gone.length) h += '<div class="muted small" style="margin-top:8px">Previously seen: ' + gone.map(function (g) { return esc(g.name || g.id) + " (" + day(g.first_seen) + "–" + day(g.last_seen) + ")"; }).join(", ") + "</div>";
    if (chatter.length) h += '<h3 class="small" style="margin:12px 0 4px">Stealth chatter</h3><ul class="mlist">' + chatter.map(function (i) {
      return '<li><a href="' + esc(i.url) + '" target="_blank" rel="noopener noreferrer">' + esc(i.title) + '</a><span class="meta">' + day(i.ts) + "</span></li>";
    }).join("") + "</ul>";
    $("#n-stealth").innerHTML = h;

    var nm = m.new || [];
    $("#n-new").innerHTML = nm.length ? '<ul class="mlist">' + nm.slice(0, 10).map(function (x) {
      return "<li><div>" + star(x) + "<b>" + esc(x.name) + '</b><div class="mid">' + esc(x.id) + (x.q ? " · quality " + x.q : "") + '</div></div><div class="meta">' +
        day(x.created) + "<br>" + usd(x.blended) + "/M" + (x.ctx ? " · " + Math.round(x.ctx / 1000) + "k" : "") + "<br>" + pinBtn(x.id) + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">Waiting for the first catalogue fetch…</div>';

    renderValue(m);

    var rt = m.retiring || [];
    $("#n-retiring").innerHTML = rt.length ? '<ul class="mlist">' + rt.slice(0, 8).map(function (x) {
      var days = Math.round((x.exp_ts - Date.now() / 1000) / 86400);
      return "<li><div>" + star(x) + esc(x.name) + '<div class="mid">' + esc(x.id) + '</div></div><div class="meta">' + esc(x.exp) + "<br>" +
        (days <= 7 ? '<span class="warnline">⚠ ' + Math.max(0, days) + " days</span>" : days + " days") + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">Nothing scheduled for retirement.</div>';

    var pc = m.price_changes || [];
    $("#n-prices").innerHTML = pc.length ? '<ul class="mlist">' + pc.slice(0, 8).map(function (c) {
      var down = c.pct < 0;
      return "<li><div>" + star(c) + esc(c.name) + '<div class="mid">' + esc(c.id) + '</div></div><div class="meta">' + usd(c.old) + " → " + usd(c.new) + "/M<br>" +
        '<span class="' + (down ? "down" : "up") + '">' + (down ? "▼ " : "▲ ") + Math.abs(c.pct) + "%</span> · " + day(c.ts) + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">No price changes yet — the Pi compares the catalogue day to day.</div>';

    renderFamilies();
    bindPins($("#view-models"));
  }

  function trackedIds() { return ((N.data && N.data.models && N.data.models.tracked) || []).map(function (t) { return t.id; }); }
  function pinBtn(id) {
    var on = trackedIds().indexOf(id) >= 0;
    return '<button class="btn ghost small" data-pin="' + esc(id) + '" aria-pressed="' + on + '">' + (on ? "Pinned ✓" : "Pin") + "</button>";
  }
  function bindPins(root) {
    root.querySelectorAll("[data-pin]").forEach(function (b) {
      if (b._bound) return; b._bound = true;
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var id = b.getAttribute("data-pin"), on = b.getAttribute("aria-pressed") === "true";
        b.disabled = true;
        api("/api/models/" + (on ? "untrack" : "track"), { id: id }).then(function () {
          toast(on ? "Unpinned" : "Pinned — price, retirement and new versions are now tracked");
          return loadModels();
        }).catch(function (err) { toast(err.message); b.disabled = false; });
      });
    });
  }

  function renderTracked(rows) {
    var el = $("#m-tracked");
    if (!rows.length) { el.innerHTML = '<div class="empty-note">Nothing pinned yet. Search above, or use “Pin” on any model in the lists below.</div>'; return; }
    el.innerHTML = '<div class="trk">' + rows.map(function (r) {
      var figs = [];
      if (r.status === "missing") figs.push('<span class="warnline">No longer listed on OpenRouter</span>');
      else {
        figs.push("In <b>" + usd(r["in"] != null ? r["in"] * 1e6 : null) + "</b>/M");
        figs.push("Out <b>" + usd(r.out != null ? r.out * 1e6 : null) + "</b>/M");
        if (r.q) figs.push("Quality <b>" + r.q + "</b>");
        if (r.ctx) figs.push("<b>" + Math.round(r.ctx / 1000) + "k</b> ctx");
        if (r.change_pct != null) figs.push('<span class="' + (r.change_pct < 0 ? "down" : "up") + '">' + (r.change_pct < 0 ? "▼ " : "▲ ") + Math.abs(r.change_pct) + "% since pinned</span>");
        if (r.retire_days != null) figs.push('<span class="' + (r.retire_days <= 7 ? "warnline" : "") + '">retires in ' + r.retire_days + " d</span>");
        if (r.newer) figs.push("<span>newer version: <b>" + esc(r.newer) + "</b></span>");
      }
      return '<div class="trk-row"><div><div class="n">' + esc(r.name) + '</div><div class="mid muted small">' + esc(r.id) + " · pinned " + day(r.added_ts) + '</div></div>' +
        '<div class="figs">' + figs.join("") + "</div>" + pinBtn(r.id) + "</div>";
    }).join("") + "</div>";
  }

  function renderFamilies() {
    var el = $("#m-families"), idx = N.index, w = (N.data && N.data.watch) || {};
    if (!idx) { el.innerHTML = '<div class="empty-note">Loading families…</div>'; return; }
    var fams = w.families || [];
    var shown = idx.authors.filter(function (a) { return a[1] >= 3 || fams.indexOf(a[0]) >= 0; }).slice(0, 40);
    el.innerHTML = shown.map(function (a) {
      var on = fams.indexOf(a[0]) >= 0;
      return '<button class="chip" data-fam="' + esc(a[0]) + '" aria-pressed="' + on + '">' + (on ? "★ " : "") + esc(a[0]) + '<span class="n">' + a[1] + "</span></button>";
    }).join("");
    el.querySelectorAll("[data-fam]").forEach(function (b) {
      b.addEventListener("click", function () {
        var f = b.getAttribute("data-fam"), list = fams.slice(), i = list.indexOf(f);
        if (i >= 0) list.splice(i, 1); else list.push(f);
        api("/api/settings", { news_watch: { families: list } }).then(function () {
          toast("Watch list updated — refreshing the model lists");
          setTimeout(loadModels, 2500);
        }).catch(function (e) { toast(e.message); });
      });
    });
  }

  // in-browser search over the slim index (no per-keystroke work on the Pi)
  function bindSearch() {
    var input = $("#m-search"), list = $("#m-results"), sel = -1, hits = [];
    var draw = function () {
      list.innerHTML = hits.map(function (m, i) {
        return '<li role="option" data-id="' + esc(m.id) + '" aria-selected="' + (i === sel) + '"><div><div>' + esc(m.n) + '</div><div class="mid">' + esc(m.id) + "</div></div>" +
          '<span class="muted small">' + usd(m.b) + "/M" + (m.q ? " · " + m.q : "") + "</span></li>";
      }).join("");
      list.hidden = !hits.length;
      list.querySelectorAll("li").forEach(function (li) {
        li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(li.getAttribute("data-id")); });
      });
    };
    var pick = function (id) {
      input.value = ""; hits = []; draw();
      api("/api/models/track", { id: id }).then(function () { toast("Pinned " + id); loadModels(); }).catch(function (e) { toast(e.message); });
    };
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      if (!q || !N.index) { hits = []; draw(); return; }
      var words = q.split(/\s+/), pinned = trackedIds();
      hits = N.index.models.filter(function (m) {
        var hay = (m.id + " " + m.n).toLowerCase();
        return pinned.indexOf(m.id) < 0 && words.every(function (w) { return hay.indexOf(w) >= 0; });
      }).sort(function (a, b) { return (b.q || 0) - (a.q || 0); }).slice(0, 12);
      sel = hits.length ? 0 : -1; draw();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { sel = Math.min(hits.length - 1, sel + 1); draw(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); draw(); e.preventDefault(); }
      else if (e.key === "Enter" && hits[sel]) { pick(hits[sel].id); e.preventDefault(); }
      else if (e.key === "Escape") { hits = []; draw(); }
    });
    input.addEventListener("blur", function () { setTimeout(function () { list.hidden = true; }, 150); });
    input.addEventListener("focus", function () { if (hits.length) list.hidden = false; });
  }

  function renderValue(m) {
    var el = $("#n-value"), tiers = m.value_tiers || [], frontier = m.frontier || [], btn = $("#bb-toggle");
    btn.textContent = N.valueTable ? "Chart" : "Table";
    btn.setAttribute("aria-pressed", String(N.valueTable));
    var tierHtml = '<div class="tiers">' + tiers.map(function (t) {
      return '<div class="tier"><h3>' + esc(t.label) + "</h3>" + (t.models.length ? "<ol>" + t.models.map(function (x) {
        return "<li>" + star(x) + esc(x.name) + ' <span class="q">' + x.q + " · " + usd(x.blended) + "</span></li>";
      }).join("") + "</ol>" : '<span class="muted small">none rated</span>') + "</div>";
    }).join("") + "</div>";
    if (!frontier.length) { el.innerHTML = '<div class="empty-note">Waiting for the first catalogue fetch…</div>'; return; }
    if (N.valueTable) {
      el.innerHTML = '<div class="table-scroll"><table><thead><tr><th>Frontier model</th><th class="num">Quality (Elo)</th><th class="num">$ / 1M</th></tr></thead><tbody>' +
        frontier.map(function (f) { return "<tr><td>" + star(f) + esc(f.name) + '</td><td class="num">' + f.q + '</td><td class="num">' + usd(f.blended) + "</td></tr>"; }).join("") +
        "</tbody></table></div>" + tierHtml;
      return;
    }
    var pts = {}, add = function (x, onF) {
      if (!x.blended || x.blended <= 0 || !x.q) return;
      var k = x.id; pts[k] = pts[k] || { id: x.id, name: x.name, q: x.q, p: x.blended, you: !!x.watched, f: false };
      if (onF) pts[k].f = true;
    };
    tiers.forEach(function (t) { t.models.forEach(function (x) { add(x, false); }); });
    frontier.forEach(function (x) { add(x, true); });
    var list = Object.keys(pts).map(function (k) { return pts[k]; });
    var topQ = Math.max.apply(null, list.map(function (p) { return p.q; }));
    var hidden = list.filter(function (p) { return p.q < topQ - 250; }).length;
    list = list.filter(function (p) { return p.q >= topQ - 250; });
    // Draw at the container's real width so 11px labels stay 11px on a phone.
    var W = Math.max(300, Math.min(900, el.clientWidth || 760)), H = W < 520 ? 260 : 300, L = 42, R = 10, T = 12, B = 30;
    var lp = list.map(function (p) { return Math.log10(p.p); }), qs = list.map(function (p) { return p.q; });
    var x0 = Math.floor(Math.min.apply(null, lp)), x1 = Math.ceil(Math.max.apply(null, lp));
    var y0 = Math.floor(Math.min.apply(null, qs) / 50) * 50, y1 = Math.ceil(Math.max.apply(null, qs) / 50) * 50;
    var sx = function (p) { return L + (Math.log10(p) - x0) / Math.max(0.001, x1 - x0) * (W - L - R); };
    var sy = function (q) { return T + (1 - (q - y0) / Math.max(1, y1 - y0)) * (H - T - B); };
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Model quality against price">';
    for (var e = x0; e <= x1; e++) {
      var gx = sx(Math.pow(10, e));
      svg += '<line x1="' + gx + '" x2="' + gx + '" y1="' + T + '" y2="' + (H - B) + '" stroke="var(--grid)"/>' +
        '<text x="' + gx + '" y="' + (H - 10) + '" text-anchor="middle" font-size="11" fill="var(--muted)">' + usd(Math.pow(10, e)) + "</text>";
    }
    for (var q = y0; q <= y1; q += 50) {
      svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + sy(q) + '" y2="' + sy(q) + '" stroke="var(--' + (q === y0 ? "axis" : "grid") + ')"/>' +
        '<text x="' + (L - 6) + '" y="' + (sy(q) + 4) + '" text-anchor="end" font-size="11" fill="var(--muted)">' + q + "</text>";
    }
    var fl = list.filter(function (p) { return p.f; }).sort(function (a, b) { return a.p - b.p; });
    if (fl.length > 1) svg += '<path d="M' + fl.map(function (p) { return sx(p.p).toFixed(1) + "," + sy(p.q).toFixed(1); }).join(" L") +
      '" fill="none" stroke="var(--ink-2)" stroke-width="2" stroke-linejoin="round"/>';
    var labels = [], marks = "";
    list.sort(function (a, b) { return a.you - b.you; }).forEach(function (p) {
      var cx = sx(p.p), cy = sy(p.q);
      var tipH = "<b>" + esc(p.name) + "</b><br>Quality " + p.q + " · " + usd(p.p) + " per 1M" + (p.f ? "<br>On the frontier" : "") + (p.you ? "<br>★ a family you watch" : "");
      marks += '<g tabindex="0" data-tip="' + tipH.replace(/"/g, "&quot;") + '"><circle cx="' + cx + '" cy="' + cy + '" r="14" fill="transparent"/>' +
        '<circle cx="' + cx + '" cy="' + cy + '" r="5" fill="var(--' + (p.you ? "ink" : "axis") + ')" stroke="var(--surface)" stroke-width="2"/></g>';
    });
    fl.slice().reverse().forEach(function (p) {
      var text = p.name.replace(/^[^:]+:\s*/, ""), w = text.length * 6.2, cx = sx(p.p), cy = sy(p.q);
      var cands = [[cx + 8, cy - 8], [cx + 8, cy + 16], [cx - 8 - w, cy - 8], [cx - 8 - w, cy + 16]];
      for (var i = 0; i < cands.length; i++) {
        var x = cands[i][0], y = cands[i][1];
        if (x < L || x + w > W - R || y < T + 8 || y > H - B - 2) continue;
        var clash = labels.some(function (b) { return x < b.x + b.w + 4 && b.x < x + w + 4 && Math.abs(y - b.y) < 13; });
        if (!clash) {
          labels.push({ x: x, y: y, w: w });
          svg += '<text x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" font-size="11" fill="var(--ink-2)" stroke="var(--surface)" stroke-width="4" paint-order="stroke" stroke-linejoin="round">' + esc(text) + "</text>";
          break;
        }
      }
    });
    svg += marks + "</svg>";
    el.innerHTML = '<div class="value-chart">' + svg + "</div>" +
      '<div class="muted small inline" style="gap:16px;margin-top:4px"><span><span class="swatch" style="background:var(--ink);border-radius:50%"></span>families you watch</span>' +
      '<span><span class="swatch" style="background:var(--axis);border-radius:50%"></span>others</span>' +
      '<span><span class="swatch" style="background:var(--ink-2);height:2px;vertical-align:3px"></span>frontier</span>' +
      (hidden ? "<span>" + hidden + " far-cheaper, much weaker model" + (hidden > 1 ? "s" : "") + " in the table view</span>" : "") + "</div>" + tierHtml;
    bindTips(el);
  }

  function loadModels() {
    var v = $("#view-models");
    if (N.data) v.classList.add("loading");
    var need = N.index ? Promise.resolve(N.index) : api("/api/models/index").then(function (x) {
      N.index = x;
      $("#m-search").placeholder = "Search " + x.models.length + " models to pin… e.g. sonnet, deepseek, gemini flash";
      return x;
    });
    return Promise.all([loadNewsData(), need]).then(renderModels)
      .catch(function (e) { $("#models-updated").textContent = "Couldn't load models: " + e.message; })
      .then(function () { v.classList.remove("loading"); });
  }
  function loadNews() {
    var v = $("#view-news");
    if (N.data) v.classList.add("loading");
    return loadNewsData().then(renderNews)
      .catch(function (e) { $("#news-errors").textContent = "Couldn't load news: " + e.message; })
      .then(function () { v.classList.remove("loading"); });
  }

  // ================================================================== settings
  var PLATFORM_OPTS = null;
  function platformSelect(val) {
    var opts = (PLATFORM_OPTS || []).concat([{ id: "anthropic_api", name: "Anthropic API (console)" }, { id: "other", name: "Other" }]);
    return '<select data-f="platform">' + opts.map(function (p) {
      return '<option value="' + p.id + '"' + (p.id === val ? " selected" : "") + ">" + esc(p.name) + "</option>";
    }).join("") + "</select>";
  }
  function curSelect(val, field) {
    var list = (S.settings && S.settings.currencies) || ["USD", "GBP", "EUR"];
    return '<select data-f="' + (field || "currency") + '">' + list.map(function (c) {
      return '<option value="' + c + '"' + (c === val ? " selected" : "") + ">" + c + "</option>";
    }).join("") + "</select>";
  }
  var KEY_INFO = {
    openrouter_api_key: ["OpenRouter API key", "openrouter.ai/settings/keys — credit balance, daily/weekly/monthly spend."],
    opencode_api_key: ["OpenCode Go API key", "opencode.ai — your Go key; reads 5h / weekly / monthly usage."],
    moonshot_api_key: ["Moonshot (Kimi API) key", "platform.moonshot.ai — prepaid balance; spend = balance drops."],
    kimi_code_api_key: ["Kimi Code API key", "kimi.com/code console — 5h / weekly coding-plan usage."],
    gemini_api_key: ["Gemini API key", "aistudio.google.com — key health only (Google has no usage API for keys)."]
  };
  var OAUTH_INFO = {
    claude: ["Claude (Pro/Max)", "Creates a separate login for this dashboard (forced fresh sign-in) — check Claude Code and other tools are still signed in after the first connect."],
    codex: ["ChatGPT / Codex", "Creates a separate login for the Codex usage windows — check Codex CLI and other tools are still signed in afterwards."],
    antigravity: ["Antigravity (Google)", "Google account used in Antigravity — quota windows per model family."]
  };
  function loadSettings() {
    return api("/api/settings").then(function (s) { S.settings = s; PLATFORM_OPTS = s.platforms; renderSettings(); })
      .catch(function (e) { $("#settings").innerHTML = '<div class="card err">' + esc(e.message) + "</div>"; });
  }
  function renderSettings() {
    var s = S.settings, c = s.config, el = $("#settings");
    var html = '<div class="settings">';
    html += '<section class="card"><h2>Subscription logins</h2><p class="help">Each login belongs to this dashboard only. Tokens stay in <code>data/secrets.json</code> and are never sent to the browser.</p>';
    ["claude", "codex", "antigravity"].forEach(function (p) {
      var o = s.oauth[p] || {}, info = OAUTH_INFO[p];
      var status = o.connected ? (o.needs_login ? '<span class="warnline">⚠ Login expired</span>' : "✓ Connected" + (o.account ? " — " + esc(o.account) : "")) : '<span class="muted">Not connected</span>';
      html += '<div class="form-row"><label>' + info[0] + "</label><div>" + status + '</div><div class="inline">' +
        '<button class="btn small" data-oauth-start="' + p + '">' + (o.connected ? "Reconnect" : "Connect") + "</button>" +
        (p === "codex" ? '<button class="btn small ghost" data-codex-device>Use device code</button>' : "") +
        (o.connected ? '<button class="btn small ghost danger" data-oauth-logout="' + p + '">Disconnect</button>' : "") +
        '</div><div class="hint">' + info[1] + '</div><div class="oauth-box" id="ob-' + p + '" hidden></div></div>';
    });
    html += '<div class="form-row"><label>Antigravity OAuth client</label><div class="inline">' +
      '<input type="text" id="ag-cid" placeholder="client ID (…apps.googleusercontent.com)" value="' + esc(c.antigravity_client_id || "") + '">' +
      '<input type="password" id="ag-sec" placeholder="' + (s.antigravity_client_secret_set ? "client secret set — leave blank to keep" : "client secret") + '"></div>' +
      '<button class="btn small" id="ag-save">Save</button><div class="hint">The public desktop-app client the Antigravity app ships with. Without it, skip Antigravity.</div></div>';
    html += "</section>";
    html += '<section class="card"><h2>API keys</h2><p class="help">Stored only on this device. Leave a field blank to keep the current key; use “Remove” to delete it.</p>';
    Object.keys(KEY_INFO).forEach(function (k) {
      var cur = s.secrets[k];
      html += '<div class="form-row"><label for="k-' + k + '">' + KEY_INFO[k][0] + '</label><input type="password" autocomplete="off" id="k-' + k + '" placeholder="' +
        (cur ? "saved: " + esc(cur) : "not set") + '"><div class="inline"><button class="btn small" data-key-save="' + k + '">Save</button>' +
        (cur ? '<button class="btn small ghost danger" data-key-del="' + k + '">Remove</button>' : "") + '</div><div class="hint">' + KEY_INFO[k][1] + "</div></div>";
    });
    html += '<div class="form-row"><label for="ms-base">Moonshot region</label><select id="ms-base">' +
      ["https://api.moonshot.ai/v1", "https://api.moonshot.cn/v1"].map(function (u) {
        return "<option" + (c.moonshot_base_url === u ? " selected" : "") + ">" + u + "</option>";
      }).join("") + '</select><button class="btn small" id="ms-save">Save</button></div></section>';
    html += '<section class="card"><h2>Subscriptions</h2><p class="help">Fixed monthly charges, counted every month from <i>start month</i>. Leave the amount empty to use the list price of the plan detected from your login or the provider’s public pricing page; type your real price to override.</p>' +
      '<div class="table-scroll"><table class="edit-table" id="subs-table"><thead><tr><th>Name</th><th>Platform</th><th>Amount</th><th>Currency</th><th>Start month</th><th>Active</th><th></th></tr></thead><tbody>' +
      (c.subscriptions || []).map(subRow).join("") + '</tbody></table></div><div class="inline" style="margin-top:10px">' +
      '<button class="btn small ghost" id="sub-add">+ Add subscription</button><button class="btn primary small" id="subs-save">Save subscriptions</button></div></section>';
    var nMan = (c.manual_entries || []).length;
    html += '<details class="card advanced"' + (S.advOpen ? " open" : "") + '><summary><h2>Advanced: invoices &amp; manual entries</h2>' +
      '<span class="muted small">' + (nMan ? nMan + " entr" + (nMan === 1 ? "y" : "ies") : "not needed most months — spend is tracked automatically") + "</span></summary>" +
      '<p class="help">Only for costs no API reports (Gemini API billing, Anthropic console credits, Claude extra-usage <i>purchases</i>) or to correct a month (e.g. a prorated upgrade). ' +
      "<b>Entries for a platform and month replace the automatic figure for that column.</b> " +
      "“spend” adds to the total · “bought” goes in the Bought column · “bought &amp; used” counts in both · “subscription charge” replaces that month's subscription.</p>" +
      '<div class="table-scroll"><table class="edit-table" id="man-table"><thead><tr><th>Month</th><th>Platform</th><th>Type</th><th>Amount</th><th>Currency</th><th>Note</th><th></th></tr></thead><tbody>' +
      (c.manual_entries || []).map(manRow).join("") + '</tbody></table></div><div class="inline" style="margin-top:10px">' +
      '<button class="btn small ghost" id="man-add">+ Add entry</button><button class="btn primary small" id="man-save">Save entries</button></div></details>';
    html += '<section class="card"><h2>News &amp; models</h2>' +
      '<div class="form-row"><label for="nw-hn">Hacker News searches</label><input type="text" id="nw-hn" value="' +
        esc(((c.news_watch && c.news_watch.hn_queries) || ["Claude", "Anthropic", "OpenAI Codex", "GPT", "Gemini", "Kimi", "DeepSeek", "OpenRouter"]).join(", ")) + '">' +
      '<button class="btn small" id="nw-save">Save</button><div class="hint">Stories from the last 14 days with ≥ 60 points whose title mentions one of these. Model families and pinned models are on the <a href="#/models">Models</a> page.</div></div></section>';
    html += '<section class="card"><h2>General</h2>' +
      '<div class="form-row"><label>Polling</label><div class="inline">' + Object.keys(c.intervals).filter(function (k) { return ["fx", "news", "catalog", "prices"].indexOf(k) < 0; }).map(function (k) {
        var on = c.enabled[k] !== false;
        return '<label class="inline small"><input type="checkbox" data-en="' + k + '"' + (on ? " checked" : "") + "> " + esc(k) +
          ' <input type="number" min="60" step="30" style="width:84px" data-iv="' + k + '" value="' + c.intervals[k] + '">s</label>';
      }).join("") + '</div><button class="btn small" id="poll-save">Save</button><div class="hint">Claude is never polled faster than every 300 s (its usage endpoint rate-limits).</div></div>' +
      '<div class="form-row"><label for="admin-pw">Admin password</label><input type="password" id="admin-pw" placeholder="' + (s.admin_required ? "set — enter new to change, blank + save to remove" : "none — anyone on your LAN can change settings") + '">' +
      '<button class="btn small" id="admin-save">Save</button><div class="hint">Protects settings, logins and pins. The dashboard itself stays readable on your LAN.</div></div></section>';
    html += "</div>";
    el.innerHTML = html;
    bindSettings();
  }
  function autoHint(platform) {
    var d = ((S.settings && S.settings.config.detected_plans) || {})[platform];
    return d && d.plan ? "auto: " + d.plan + " list price" : "0 = not counted";
  }
  function subRow(sub) {
    sub = sub || { id: "", name: "", platform: "other", amount: 0, currency: (S.state && S.state.currency) || "GBP", active: true, start_month: "" };
    return '<tr data-id="' + esc(sub.id) + '"><td><input type="text" data-f="name" value="' + esc(sub.name) + '"></td><td>' + platformSelect(sub.platform) +
      '</td><td><input type="number" step="0.01" min="0" data-f="amount" value="' + (sub.amount ? sub.amount : "") + '" placeholder="' + esc(autoHint(sub.platform)) + '"></td><td>' + curSelect(sub.currency) +
      '</td><td><input type="month" data-f="start_month" value="' + esc(sub.start_month || "") + '"></td><td><input type="checkbox" data-f="active"' + (sub.active !== false ? " checked" : "") +
      '></td><td><button class="btn small ghost danger" data-del-row aria-label="Remove">✕</button></td></tr>';
  }
  function manRow(e) {
    var cm = (S.state && S.state.current_month) || "";
    e = e || { id: "", month: cm, platform: "gemini", kind: "spend", amount: 0, currency: "USD", note: "" };
    return '<tr data-id="' + esc(e.id) + '"><td><input type="month" data-f="month" value="' + esc(e.month) + '"></td><td>' + platformSelect(e.platform) +
      '</td><td><select data-f="kind">' + [["spend", "spend"], ["topup", "bought"], ["credit", "bought & used"], ["subscription", "subscription charge"]].map(function (k) {
        return '<option value="' + k[0] + '"' + ((e.kind || "spend") === k[0] ? " selected" : "") + ">" + k[1] + "</option>";
      }).join("") + '</select></td><td><input type="number" step="0.01" min="0" data-f="amount" value="' + (e.amount || 0) + '"></td><td>' + curSelect(e.currency) +
      '</td><td><input type="text" data-f="note" value="' + esc(e.note) + '"></td><td><button class="btn small ghost danger" data-del-row aria-label="Remove">✕</button></td></tr>';
  }
  function readRows(tableSel) {
    return Array.prototype.map.call(document.querySelectorAll(tableSel + " tbody tr"), function (tr) {
      var o = { id: tr.getAttribute("data-id") || undefined };
      tr.querySelectorAll("[data-f]").forEach(function (i) {
        var f = i.getAttribute("data-f");
        o[f] = i.type === "checkbox" ? i.checked : (i.type === "number" ? parseFloat(i.value || "0") : i.value);
      });
      return o;
    });
  }
  function saveSettings(body, msg) {
    return api("/api/settings", body).then(function (s) { S.settings = s; renderSettings(); toast(msg || "Saved"); refresh(); })
      .catch(function (e) { toast("Error: " + e.message); });
  }
  function bindSettings() {
    var el = $("#settings");
    el.querySelectorAll("[data-oauth-start]").forEach(function (b) {
      b.addEventListener("click", function () {
        var p = b.getAttribute("data-oauth-start"), box = $("#ob-" + p);
        api("/api/oauth/" + p + "/start", {}).then(function (r) {
          box.hidden = false;
          box.innerHTML = '<div>1. Open: <a href="' + esc(r.auth_url) + '" target="_blank" rel="noopener noreferrer">' + esc(r.auth_url.slice(0, 90)) + "…</a></div>" +
            "<div>2. " + esc(r.instructions) + '</div><div class="inline"><input type="text" placeholder="Paste code or URL here" style="flex:1">' +
            '<button class="btn primary small">Finish login</button></div>';
          var input = box.querySelector("input");
          box.querySelector("button").addEventListener("click", function () {
            api("/api/oauth/" + p + "/finish", { code: input.value }).then(function () { toast("Connected"); loadSettings(); refresh(); })
              .catch(function (e) { toast("Login failed: " + e.message); });
          });
        }).catch(function (e) { toast(e.message); });
      });
    });
    el.querySelectorAll("[data-oauth-logout]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (!confirm("Disconnect this login?")) return;
        api("/api/oauth/" + b.getAttribute("data-oauth-logout") + "/logout", {}).then(function () { loadSettings(); refresh(); });
      });
    });
    var dev = el.querySelector("[data-codex-device]");
    if (dev) dev.addEventListener("click", function () {
      var box = $("#ob-codex");
      api("/api/oauth/codex/device", {}).then(function (r) {
        box.hidden = false;
        box.innerHTML = '<div>Open <a href="' + esc(r.verify_url) + '" target="_blank" rel="noopener noreferrer">' + esc(r.verify_url) + "</a> and enter:</div>" +
          '<div class="code">' + esc(r.user_code) + '</div><div class="muted small" id="dev-status">Waiting for you to approve…</div>';
        var poll = setInterval(function () {
          api("/api/oauth/codex/device").then(function (d) {
            if (d.state === "done") { clearInterval(poll); toast("ChatGPT connected"); loadSettings(); refresh(); }
            else if (d.state === "error") { clearInterval(poll); var st = $("#dev-status"); if (st) st.textContent = "Failed: " + d.error; }
          });
        }, 4000);
      }).catch(function (e) { box.hidden = false; box.innerHTML = '<div class="err">' + esc(e.message) + "</div>"; });
    });
    $("#ag-save").addEventListener("click", function () { saveSettings({ antigravity_client_id: $("#ag-cid").value, antigravity_client_secret: $("#ag-sec").value }); });
    el.querySelectorAll("[data-key-save]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.getAttribute("data-key-save"), v = $("#k-" + k).value.trim();
        if (!v) { toast("Paste a key first"); return; }
        var sec = {}; sec[k] = v; saveSettings({ secrets: sec }, "Key saved — checking now");
      });
    });
    el.querySelectorAll("[data-key-del]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (!confirm("Remove this key?")) return;
        var sec = {}; sec[b.getAttribute("data-key-del")] = ""; saveSettings({ secrets: sec }, "Key removed");
      });
    });
    $("#ms-save").addEventListener("click", function () { saveSettings({ moonshot_base_url: $("#ms-base").value }); });
    $("#sub-add").addEventListener("click", function () { $("#subs-table tbody").insertAdjacentHTML("beforeend", subRow()); bindDel(); });
    $("#man-add").addEventListener("click", function () { $("#man-table tbody").insertAdjacentHTML("beforeend", manRow()); bindDel(); });
    $("#subs-save").addEventListener("click", function () { saveSettings({ subscriptions: readRows("#subs-table") }, "Subscriptions saved"); });
    $("#man-save").addEventListener("click", function () { S.advOpen = true; saveSettings({ manual_entries: readRows("#man-table") }, "Entries saved"); });
    $(".advanced").addEventListener("toggle", function (e) { S.advOpen = e.target.open; });
    $("#poll-save").addEventListener("click", function () {
      var iv = {}, en = {};
      el.querySelectorAll("[data-iv]").forEach(function (i) { iv[i.getAttribute("data-iv")] = parseInt(i.value, 10); });
      el.querySelectorAll("[data-en]").forEach(function (i) { en[i.getAttribute("data-en")] = i.checked; });
      saveSettings({ intervals: iv, enabled: en });
    });
    $("#nw-save").addEventListener("click", function () {
      var list = $("#nw-hn").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      saveSettings({ news_watch: { hn_queries: list } }, "Searches saved — refreshing news");
    });
    $("#admin-save").addEventListener("click", function () {
      var v = $("#admin-pw").value;
      saveSettings({ admin_password: v }, v ? "Admin password set" : "Admin password removed").then(function () {
        try { if (v) sessionStorage.setItem("aidash.admin", v); else sessionStorage.removeItem("aidash.admin"); } catch (e) { /* ignore */ }
      });
    });
    bindDel();
  }
  function bindDel() {
    document.querySelectorAll("[data-del-row]").forEach(function (b) {
      if (b._bound) return; b._bound = true;
      b.addEventListener("click", function () { b.closest("tr").remove(); });
    });
  }

  // ================================================================== routing + boot
  var VIEWS = { "": "dash", "#/": "dash", "#/news": "news", "#/models": "models", "#/settings": "settings" };
  function route() {
    var view = VIEWS[location.hash] || "dash";
    S.view = view;
    ["dash", "news", "models", "settings"].forEach(function (v) { $("#view-" + v).hidden = v !== view; });
    document.querySelectorAll(".tabs a, .bottombar a").forEach(function (a) {
      if (a.getAttribute("data-view") === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    if (view === "settings") loadSettings();
    if (view === "news") loadNews();
    if (view === "models") loadModels();
    if (view === "dash" && S.state) render();
    hideTip();
    window.scrollTo(0, 0);
  }

  S.credits = load("credits", "used");
  N.filter = load("newsFilter", "you");
  $("#f-month").addEventListener("change", function (e) { S.month = e.target.value; refresh(); });
  $("#f-currency").addEventListener("change", function (e) { api("/api/currency", { currency: e.target.value }).then(refresh).catch(function (err) { toast(err.message); }); });
  $("#f-credits").addEventListener("change", function (e) { S.credits = e.target.value; save("credits", S.credits); render(); });
  $("#tr-toggle").addEventListener("click", function () { S.trendTable = !S.trendTable; renderTrend(S.state); });
  $("#bb-toggle").addEventListener("click", function () { N.valueTable = !N.valueTable; if (N.data) renderValue(N.data.models || {}); });
  bindSearch();
  window.addEventListener("hashchange", route);
  route();
  refresh();
  setInterval(function () {
    if (document.hidden) return;
    if (S.view === "dash") refresh();
  }, REFRESH_MS);
  setInterval(function () {
    if (document.hidden) return;
    if (S.view === "news") loadNews();
    else if (S.view === "models") loadModels();
  }, 5 * 60000);
})();
