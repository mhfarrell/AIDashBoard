/* AI usage dashboard - vanilla JS, no dependencies. Reads cached state from the Pi; never calls providers. */
(function () {
  "use strict";

  // Colour follows the platform (fixed slot per entity), never its rank.
  var SLOT = { claude: 1, codex: 2, antigravity: 3, gemini: 4, opencode: 5, openrouter: 6, moonshot: 7, kimi_code: 8 };
  var DARK_INK_SLOTS = { 4: true }; // yellow needs dark text inside a fill (light mode)
  var REFRESH_MS = 30000;

  var S = { state: null, settings: null, month: null, credits: "used", trendTable: false, open: {} };
  var $ = function (sel, el) { return (el || document).querySelector(sel); };

  function color(pid) { return SLOT[pid] ? "var(--s" + SLOT[pid] + ")" : "var(--other)"; }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function load(k, d) { try { var v = localStorage.getItem("aidash." + k); return v == null ? d : v; } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem("aidash." + k, v); } catch (e) { /* private mode */ } }

  // ------------------------------------------------------------------ formatting
  function money(v, cur) {
    if (v == null || isNaN(v)) return "—";
    cur = cur || (S.state && S.state.currency) || "USD";
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2 }).format(v);
    } catch (e) { return v.toFixed(2) + " " + cur; }
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
  function monthLabel(m) {
    var p = m.split("-");
    return new Date(+p[0], +p[1] - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
  }
  function shortMonth(m) {
    var p = m.split("-");
    return new Date(+p[0], +p[1] - 1, 1).toLocaleDateString(undefined, { month: "short" });
  }
  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.hidden = true; }, 3500);
  }

  // ------------------------------------------------------------------ API
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

  // ------------------------------------------------------------------ tooltip
  var tip = null;
  function showTip(html, ev) {
    tip = tip || $("#tooltip");
    tip.innerHTML = html; tip.hidden = false;
    var x = ev.clientX + 14, y = ev.clientY + 14, w = tip.offsetWidth, h = tip.offsetHeight;
    if (x + w > window.innerWidth - 8) x = ev.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = ev.clientY - h - 14;
    tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
  }
  function hideTip() { if (tip) tip.hidden = true; }
  function bindTips(root) {
    root.querySelectorAll("[data-tip]").forEach(function (el) {
      el.addEventListener("mousemove", function (e) { showTip(el.getAttribute("data-tip"), e); });
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("focus", function () {
        var r = el.getBoundingClientRect();
        showTip(el.getAttribute("data-tip"), { clientX: r.left + r.width / 2, clientY: r.bottom });
      });
      el.addEventListener("blur", hideTip);
    });
  }

  // ------------------------------------------------------------------ dashboard
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
    renderHero(st);
    renderBreakdown(st);
    renderActive(st);
    renderTrend(st);
    renderCards(st);
    var fx = st.fx || {};
    $("#fx-note").textContent = "Exchange rates: " + (fx.source || "?") + (fx.day ? " (" + fx.day + ")" : "") +
      (fx.error ? " — last refresh failed: " + fx.error : "") +
      ". Provider amounts are converted to " + st.currency + ". Dashboard v" + st.version + ".";
    $("#updated").textContent = "Updated " + new Date(st.now * 1000).toLocaleTimeString();
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

  function renderHero(st) {
    var t = st.costs.totals;
    var total = S.credits === "bought" ? t.total_cashflow : t.total;
    $("#hero-month").textContent = monthLabel(st.month);
    $("#hero-total").textContent = money(total);
    $("#hero-sub").textContent = S.credits === "bought"
      ? "Subscriptions + credits bought this month (cash out)."
      : "Subscriptions + credit actually spent this month, all platforms.";
    var bought = S.credits === "bought";
    // Tile 2 is whatever the total counts for credits; tile 3 is the other figure, shown but not added.
    $("#t-subs").textContent = money(t.subscriptions);
    $("#t-spend").previousElementSibling.textContent = bought ? "Credits bought" : "Credit spend";
    $("#t-spend").textContent = money(bought ? t.topups : t.credit_spend);
    $("#t-topups").previousElementSibling.innerHTML = (bought ? "Credit spend" : "Credits bought") + ' <span class="muted">(not in total)</span>';
    $("#t-topups").textContent = money(bought ? t.credit_spend : t.topups);
    var bal = 0, any = false;
    st.costs.rows.forEach(function (r) { if (r.balance_left != null) { bal += r.balance_left; any = true; } });
    $("#t-balance").textContent = any ? money(bal) : "—";

    // Explain empty credit figures instead of showing a silent £0.00.
    var CREDIT = { openrouter: "OpenRouter", moonshot: "Moonshot (Kimi API)" };
    var missing = st.platforms.filter(function (p) { return CREDIT[p.id] && p.status === "unconfigured"; })
      .map(function (p) { return CREDIT[p.id]; });
    var note = $("#credit-note"), msg = "";
    if (missing.length) {
      msg = "Credit spend and credits bought come from your prepaid platforms — " + esc(missing.join(" and ")) +
        (missing.length > 1 ? " aren't" : " isn't") + ' connected yet. <a href="#/settings">Add the API key' + (missing.length > 1 ? "s" : "") + " in Settings →</a> ";
    }
    if (!t.topups && st.month === st.current_month) {
      msg += "Purchases are detected when a balance goes up while the dashboard is watching; credits you bought earlier this month aren't visible to any API — " +
        '<a href="#/settings">add them under Manual entries</a>.';
    }
    note.innerHTML = msg;
    note.hidden = !msg;
  }

  function rowTotal(r) { return S.credits === "bought" ? r.subscription + r.topups : r.total; }

  function renderBreakdown(st) {
    var rows = st.costs.rows.slice().sort(function (a, b) { return rowTotal(b) - rowTotal(a) || a.name.localeCompare(b.name); });
    var grand = rows.reduce(function (s, r) { return s + rowTotal(r); }, 0);
    // Segmented bar: <= 6 segments, the tail folds into "Other".
    var bar = $("#segbar");
    var nonzero = rows.filter(function (r) { return rowTotal(r) > 0; });
    if (!grand) {
      bar.className = "segbar empty";
      bar.innerHTML = "No costs yet this month — add subscription prices in Settings.";
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
        var label = esc(r.name) + " " + Math.round(share) + "%";
        var dark = DARK_INK_SLOTS[SLOT[r.platform]] ? " dark-ink" : "";
        return '<div class="seg' + dark + '" tabindex="0" style="flex:' + v + ' 1 0;background:' + color(r.platform) + '" data-tip="<b>' +
          esc(r.name) + "</b><br>" + esc(money(v)) + " · " + share.toFixed(1) + '%"><span>' + (share >= 9 ? label : "") + "</span></div>";
      }).join("");
      bar.setAttribute("aria-label", "Share of monthly total: " + segs.map(function (r) {
        var v = r._v != null ? r._v : rowTotal(r); return r.name + " " + Math.round(100 * v / grand) + "%";
      }).join(", "));
      bindTips(bar);
    }
    $("#bd-note").textContent = S.credits === "bought"
      ? "Total = subscription + credits bought"
      : "Total = subscription + credit spend. Bought & credit left are shown, not added.";

    var tb = $("#bd-table tbody");
    tb.innerHTML = rows.map(function (r) {
      var open = !!S.open[r.platform];
      var usage = r.usage_pct == null ? '<span class="muted">—</span>'
        : '<span class="mini-meter" aria-hidden="true"><i style="width:' + Math.min(100, r.usage_pct) + '%"></i></span>' + pct(r.usage_pct) +
          ' <span class="muted small">' + esc(r.usage_label || "") + "</span>";
      var share = grand ? (100 * rowTotal(r) / grand).toFixed(1) + "%" : "—";
      var html = '<tr class="expander" tabindex="0" data-p="' + esc(r.platform) + '" aria-expanded="' + open + '">' +
        '<td><span class="chev">' + (r.items.length ? (open ? "▾" : "▸") : "") + '</span><span class="swatch" style="background:' + color(r.platform) + '"></span>' + esc(r.name) + "</td>" +
        '<td class="num">' + (r.subscription ? money(r.subscription) : '<span class="muted">—</span>') + "</td>" +
        '<td class="num">' + (r.credit_spend ? money(r.credit_spend) : '<span class="muted">—</span>') + "</td>" +
        '<td class="num"><b>' + money(rowTotal(r)) + "</b></td>" +
        '<td class="num">' + share + "</td>" +
        '<td class="num">' + (r.topups ? money(r.topups) : '<span class="muted">—</span>') + "</td>" +
        '<td class="num">' + (r.balance_left != null ? money(r.balance_left) : '<span class="muted">—</span>') + "</td>" +
        "<td>" + usage + "</td></tr>";
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
    // Quota percentages of different plans can't be summed: summarise the tightest one + how many are hot.
    var withUse = rows.filter(function (r) { return r.usage_pct != null; });
    var tight = withUse.slice().sort(function (a, b) { return b.usage_pct - a.usage_pct; })[0];
    var hot = withUse.filter(function (r) { return r.usage_pct >= 80; }).length;
    var usageSummary = tight
      ? '<span class="mini-meter" aria-hidden="true"><i style="width:' + Math.min(100, tight.usage_pct) + '%"></i></span>Tightest ' + pct(tight.usage_pct) +
        ' <span class="muted small">' + esc(tight.name) + " · " + esc(tight.usage_label || "") + "</span><br>" +
        '<span class="small">' + (hot ? '<span class="warnline">⚠ ' + hot + " of " + withUse.length + " platforms ≥ 80%</span>" : withUse.length + " platforms all below 80%") + "</span>"
      : '<span class="muted">—</span>';
    $("#bd-table tfoot").innerHTML = "<tr><td>All platforms</td>" +
      '<td class="num">' + money(t.subscriptions) + '</td><td class="num">' + money(t.credit_spend) + "</td>" +
      '<td class="num">' + money(S.credits === "bought" ? t.total_cashflow : t.total) + '</td><td class="num">100%</td>' +
      '<td class="num">' + money(t.topups) + '</td><td class="num">' + $("#t-balance").textContent + "</td><td>" + usageSummary + "</td></tr>";
    tb.querySelectorAll("tr.expander").forEach(function (tr) {
      var toggle = function () { var p = tr.getAttribute("data-p"); S.open[p] = !S.open[p]; renderBreakdown(S.state); };
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
  }

  function renderActive(st) {
    var el = $("#active");
    var now = Date.now() / 1000;
    var list = st.active_windows.filter(function (a) { return a.resets_at > now; });
    if (!list.length) {
      el.innerHTML = '<div class="empty-note">No 5-hour windows reported yet. They appear once Claude, ChatGPT, OpenCode, Kimi Code or Antigravity are connected.</div>';
      return;
    }
    el.innerHTML = '<div class="aw">' + list.map(function (a) {
      var timeFrac = Math.min(100, Math.max(0, 100 * (now - a.started_at) / a.window_s));
      var idle = a.used_pct === 0;
      return '<div class="aw-row" style="--pc:' + color(a.platform) + '">' +
        '<div class="aw-line"><span><span class="swatch" style="background:' + color(a.platform) + '"></span><b>' + esc(a.name) + "</b> · " + esc(a.label) + "</span>" +
        '<span class="num">' + (idle ? '<span class="muted">idle</span>' : pct(a.used_pct) + " used") + "</span></div>" +
        '<div class="aw-track" data-tip="Window started ' + esc(new Date(a.started_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })) +
        "<br>" + esc(until(a.resets_at)) + "<br>Grey = time elapsed, colour = quota used" + '" tabindex="0">' +
        '<div class="aw-time" style="width:' + timeFrac + '%"></div><div class="aw-use" style="width:' + Math.min(100, a.used_pct) + '%"></div></div>' +
        '<div class="meter-foot"><span>' + Math.round(timeFrac) + "% of window elapsed</span><span>" + esc(until(a.resets_at)) + "</span></div></div>";
    }).join("") + "</div>";
    bindTips(el);
  }

  function renderTrend(st) {
    var el = $("#trend"), data = st.trend;
    var btn = $("#tr-toggle");
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
      var current = d.month === st.current_month;
      var r = Math.min(4, h);
      var path = h > 0 ? "M" + (cx - bw / 2) + "," + y(0) + " V" + (top + r) + " Q" + (cx - bw / 2) + "," + top + " " + (cx - bw / 2 + r) + "," + top +
        " H" + (cx + bw / 2 - r) + " Q" + (cx + bw / 2) + "," + top + " " + (cx + bw / 2) + "," + (top + r) + " V" + y(0) + " Z" : "";
      var tipHtml = "<b>" + esc(monthLabel(d.month)) + "</b><br>Subscriptions " + esc(money(d.subscriptions)) + "<br>" +
        (S.credits === "bought" ? "Bought " + esc(money(d.topups)) : "Credit spend " + esc(money(d.credit_spend))) + "<br><b>Total " + esc(money(v)) + "</b>";
      svg += '<g class="bar" tabindex="0" data-tip="' + tipHtml.replace(/"/g, "&quot;") + '">' +
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
    var pts = series.map(function (p) { return [(p[0] - t0) / span * (W - 4) + 2, H - 4 - (p[1] / 100) * (H - 8)]; });
    var d = "M" + pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" L");
    return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="' + esc(windowKey) + ' usage, last 48 hours" data-pid="' + pid + '" data-key="' + esc(windowKey) + '">' +
      '<line x1="0" x2="' + W + '" y1="' + (H - 4) + '" y2="' + (H - 4) + '" stroke="var(--axis)" stroke-width="1"/>' +
      '<path d="' + d + '" fill="none" stroke="' + color(pid) + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>' +
      '<line class="xh" x1="0" x2="0" y1="0" y2="' + H + '" stroke="var(--muted)" stroke-width="1" vector-effect="non-scaling-stroke" visibility="hidden"/></svg>';
  }

  function bindSparks(root) {
    root.querySelectorAll("svg.spark").forEach(function (svg) {
      var pid = svg.getAttribute("data-pid"), key = svg.getAttribute("data-key");
      var p = S.state.platforms.filter(function (x) { return x.id === pid; })[0];
      var series = (p && p.sparks[key]) || [];
      var xh = svg.querySelector(".xh");
      svg.addEventListener("mousemove", function (e) {
        var r = svg.getBoundingClientRect(), fx = (e.clientX - r.left) / r.width;
        var t0 = series[0][0], t1 = series[series.length - 1][0], t = t0 + fx * (t1 - t0), best = series[0];
        series.forEach(function (s) { if (Math.abs(s[0] - t) < Math.abs(best[0] - t)) best = s; });
        var x = (best[0] - t0) / Math.max(1, t1 - t0) * 296 + 2;
        xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
        showTip("<b>" + pct(best[1]) + " used</b><br>" + esc(new Date(best[0] * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })), e);
      });
      svg.addEventListener("mouseleave", function () { xh.setAttribute("visibility", "hidden"); hideTip(); });
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
        var hot = w.used_pct >= 90;
        var proj = w.projected_pct;
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
          "</b></div><div>This month<b>" + money(m.key_usage_month, "USD") + '</b></div></div><div class="muted small">Spend figures above are for this API key; the monthly tab uses account-wide credit use when larger.</div>';
      }
      if (p.id === "claude" && m.extra_usage_amount != null) {
        var xc = m.extra_usage_currency || "USD";
        html += '<div class="balances"><div>Extra usage (API, this cycle)<b>' + money(m.extra_usage_amount, xc) + "</b></div>" +
          (m.extra_usage_limit ? "<div>Cap<b>" + money(m.extra_usage_limit, xc) + "</b></div>" : "") + "</div>";
      }
      if (primary && p.sparks[primary.key]) html += sparkline(p.id, p.sparks[primary.key], primary.key);
      if (p.notes.length) html += '<ul class="notes">' + p.notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") + "</ul>";
      html += '<div class="pcard-foot"><span>' + (p.updated_at ? "Data " + ago(p.updated_at) : "No data yet") +
        (p.status === "stale" ? " (showing last good)" : "") + "</span>" +
        (p.status !== "unconfigured" && p.status !== "disabled" ? '<button class="btn ghost small" data-refresh="' + p.id + '">Refresh</button>' : "") + "</div></article>";
      return html;
    }).join("");
    el.querySelectorAll("[data-refresh]").forEach(function (b) {
      b.addEventListener("click", function () {
        b.disabled = true;
        api("/api/refresh/" + b.getAttribute("data-refresh"), {}).then(function (r) {
          var wait = Math.max(0, Math.round(r.next_poll - Date.now() / 1000));
          toast(wait > 3 ? "Refresh queued (in " + wait + "s - rate-limit guard)" : "Refreshing…");
          setTimeout(refresh, (wait + 6) * 1000);
        }).catch(function (e) { toast(e.message); });
      });
    });
    bindSparks(el);
  }

  // ------------------------------------------------------------------ settings
  var PLATFORM_OPTS = null;
  function platformSelect(val) {
    var opts = (PLATFORM_OPTS || []).concat([{ id: "anthropic_api", name: "Anthropic API (console)" }, { id: "other", name: "Other" }]);
    return "<select data-f=\"platform\">" + opts.map(function (p) {
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
    claude: ["Claude (Pro/Max)", "Creates a separate login for the Pi (forced fresh sign-in) so it shouldn't affect Claude Code or other tools — check they're still signed in after the first connect."],
    codex: ["ChatGPT / Codex", "Creates a separate login for the Pi for the Codex usage windows — check Codex CLI and other tools are still signed in afterwards."],
    antigravity: ["Antigravity (Google)", "Google account used in Antigravity — quota windows per model family."]
  };

  function loadSettings() {
    return api("/api/settings").then(function (s) {
      S.settings = s; PLATFORM_OPTS = s.platforms; renderSettings();
    }).catch(function (e) { $("#settings").innerHTML = '<div class="card err">' + esc(e.message) + "</div>"; });
  }

  function renderSettings() {
    var s = S.settings, c = s.config, el = $("#settings");
    var html = '<div class="settings">';
    html += '<div class="inline"><a class="btn ghost" href="#/">← Dashboard</a></div>';

    // OAuth accounts
    html += '<section class="card"><h2>Subscription logins</h2><p class="help">Each login below belongs to the Pi only. Tokens stay in <code>data/secrets.json</code> on the Pi and are never sent to this browser.</p>';
    ["claude", "codex", "antigravity"].forEach(function (p) {
      var o = s.oauth[p] || {}, info = OAUTH_INFO[p];
      var status = o.connected ? (o.needs_login ? '<span class="warnline">⚠ Login expired</span>' : "✓ Connected" + (o.account ? " — " + esc(o.account) : "")) : '<span class="muted">Not connected</span>';
      html += '<div class="form-row"><label>' + info[0] + "</label><div>" + status + "</div><div class=\"inline\">" +
        '<button class="btn small" data-oauth-start="' + p + '">' + (o.connected ? "Reconnect" : "Connect") + "</button>" +
        (p === "codex" ? '<button class="btn small ghost" data-codex-device>Use device code</button>' : "") +
        (o.connected ? '<button class="btn small ghost danger" data-oauth-logout="' + p + '">Disconnect</button>' : "") +
        '</div><div class="hint">' + info[1] + '</div><div class="oauth-box" id="ob-' + p + '" hidden></div></div>';
    });
    html += '<div class="form-row"><label>Antigravity OAuth client</label><div class="inline">' +
      '<input type="text" id="ag-cid" placeholder="client ID (…apps.googleusercontent.com)" value="' + esc(c.antigravity_client_id || "") + '">' +
      '<input type="password" id="ag-sec" placeholder="' + (s.antigravity_client_secret_set ? "client secret set — leave blank to keep" : "client secret") + '"></div>' +
      '<button class="btn small" id="ag-save">Save</button><div class="hint">The public desktop-app client Antigravity itself uses (pre-filled if found during setup).</div></div>';
    html += "</section>";

    // API keys
    html += '<section class="card"><h2>API keys</h2><p class="help">Stored only on the Pi. Leave a field blank to keep the current key; use “Remove” to delete it.</p>';
    Object.keys(KEY_INFO).forEach(function (k) {
      var cur = s.secrets[k];
      html += '<div class="form-row"><label for="k-' + k + '">' + KEY_INFO[k][0] + '</label><input type="password" autocomplete="off" id="k-' + k + '" placeholder="' +
        (cur ? "saved: " + esc(cur) : "not set") + '"><div class="inline"><button class="btn small" data-key-save="' + k + '">Save</button>' +
        (cur ? '<button class="btn small ghost danger" data-key-del="' + k + '">Remove</button>' : "") + '</div><div class="hint">' + KEY_INFO[k][1] + "</div></div>";
    });
    html += '<div class="form-row"><label for="ms-base">Moonshot region</label><select id="ms-base">' +
      ['https://api.moonshot.ai/v1', 'https://api.moonshot.cn/v1'].map(function (u) {
        return '<option' + (c.moonshot_base_url === u ? " selected" : "") + ">" + u + "</option>";
      }).join("") + '</select><button class="btn small" id="ms-save">Save</button></div></section>';

    // Subscriptions
    html += '<section class="card"><h2>Subscriptions</h2><p class="help">Fixed monthly charges, counted every month from <i>start month</i>. Leave the amount empty to use the list price of the plan detected from your login (Claude, ChatGPT); type your real price to override. Unticking “active” ends it after the current month.</p>' +
      '<div class="table-scroll"><table class="edit-table" id="subs-table"><thead><tr><th>Name</th><th>Platform</th><th>Amount</th><th>Currency</th><th>Start month</th><th>Active</th><th></th></tr></thead><tbody>' +
      (c.subscriptions || []).map(subRow).join("") + '</tbody></table></div><div class="inline" style="margin-top:8px">' +
      '<button class="btn small ghost" id="sub-add">+ Add subscription</button><button class="btn primary small" id="subs-save">Save subscriptions</button></div></section>';

    // Manual entries
    html += '<section class="card"><h2>Invoices & manual entries</h2><p class="help">Real charges from your invoices, or costs no API reports (e.g. Gemini API billing). ' +
      "<b>Entries for a platform and month replace the automatic figure for that column</b> (invoices beat estimates). " +
      "“spend” adds to the total · “bought” goes in the Bought column · “bought &amp; used” = prepaid credit you've spent (both) · “subscription charge” replaces that month's subscription.</p>" +
      '<div class="table-scroll"><table class="edit-table" id="man-table"><thead><tr><th>Month</th><th>Platform</th><th>Type</th><th>Amount</th><th>Currency</th><th>Note</th><th></th></tr></thead><tbody>' +
      (c.manual_entries || []).map(manRow).join("") + '</tbody></table></div><div class="inline" style="margin-top:8px">' +
      '<button class="btn small ghost" id="man-add">+ Add entry</button><button class="btn primary small" id="man-save">Save entries</button></div></section>';

    // General
    html += '<section class="card"><h2>General</h2>' +
      '<div class="form-row"><label for="nw-fam">News: model families</label><input type="text" id="nw-fam" value="' +
        esc(((c.news_watch && c.news_watch.families) || ["anthropic", "openai", "google", "moonshotai", "deepseek", "qwen", "z-ai"]).join(", ")) + '">' +
      '<button class="btn small" id="nw-save">Save</button><div class="hint">OpenRouter author prefixes marked ★ (e.g. anthropic, openai, google, moonshotai, deepseek, qwen, z-ai, x-ai, mistralai).</div></div>' +
      '<div class="form-row"><label for="nw-hn">News: Hacker News searches</label><input type="text" id="nw-hn" value="' +
        esc(((c.news_watch && c.news_watch.hn_queries) || ["Claude", "Anthropic", "OpenAI Codex", "GPT", "Gemini", "Kimi", "DeepSeek", "OpenRouter"]).join(", ")) + '">' +
      '<span></span><div class="hint">Stories from the last 14 days with ≥ 60 points whose title mentions these.</div></div>' +
      '<div class="form-row"><label>Polling</label><div class="inline">' + Object.keys(c.intervals).filter(function (k) { return ["fx", "news", "catalog"].indexOf(k) < 0; }).map(function (k) {
        var on = c.enabled[k] !== false;
        return '<label class="ctl"><input type="checkbox" data-en="' + k + '"' + (on ? " checked" : "") + "> " + esc(k) +
          ' <input type="number" min="60" step="30" style="width:80px" data-iv="' + k + '" value="' + c.intervals[k] + '">s</label>';
      }).join("") + '</div><button class="btn small" id="poll-save">Save</button><div class="hint">Claude is never polled faster than every 300 s (its usage endpoint rate-limits).</div></div>' +
      '<div class="form-row"><label for="admin-pw">Admin password</label><input type="password" id="admin-pw" placeholder="' + (s.admin_required ? "set — enter new to change, blank + save to remove" : "none — anyone on your LAN can change settings") + '">' +
      '<button class="btn small" id="admin-save">Save</button><div class="hint">Protects settings and logins. The dashboard itself stays readable on your LAN.</div></div></section>';
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
      '</td><td><input type="number" step="0.01" min="0" data-f="amount" value="' + (sub.amount ? sub.amount : "") +
      '" placeholder="' + esc(autoHint(sub.platform)) + '"></td><td>' + curSelect(sub.currency) +
      '</td><td><input type="month" data-f="start_month" value="' + esc(sub.start_month || "") + '"></td><td><input type="checkbox" data-f="active"' + (sub.active !== false ? " checked" : "") +
      '></td><td><button class="btn small ghost danger" data-del-row>✕</button></td></tr>';
  }
  function manRow(e) {
    var cm = (S.state && S.state.current_month) || "";
    e = e || { id: "", month: cm, platform: "gemini", kind: "spend", amount: 0, currency: "USD", note: "" };
    return '<tr data-id="' + esc(e.id) + '"><td><input type="month" data-f="month" value="' + esc(e.month) + '"></td><td>' + platformSelect(e.platform) +
      '</td><td><select data-f="kind">' + [["spend", "spend"], ["topup", "bought"], ["credit", "bought & used"], ["subscription", "subscription charge"]].map(function (k) {
        return '<option value="' + k[0] + '"' + ((e.kind || "spend") === k[0] ? " selected" : "") + ">" + k[1] + "</option>";
      }).join("") + '</select></td><td><input type="number" step="0.01" min="0" data-f="amount" value="' + (e.amount || 0) + '"></td><td>' + curSelect(e.currency) +
      '</td><td><input type="text" data-f="note" value="' + esc(e.note) + '"></td><td><button class="btn small ghost danger" data-del-row>✕</button></td></tr>';
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
          box.innerHTML = "<div>1. Open: <a href=\"" + esc(r.auth_url) + "\" target=\"_blank\" rel=\"noopener noreferrer\">" + esc(r.auth_url.slice(0, 90)) + "…</a></div>" +
            "<div>2. " + esc(r.instructions) + "</div><div class=\"inline\"><input type=\"text\" placeholder=\"Paste code or URL here\" style=\"flex:1\">" +
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
        if (!confirm("Disconnect this login from the Pi?")) return;
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
    $("#ag-save").addEventListener("click", function () {
      saveSettings({ antigravity_client_id: $("#ag-cid").value, antigravity_client_secret: $("#ag-sec").value });
    });
    el.querySelectorAll("[data-key-save]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.getAttribute("data-key-save"), v = $("#k-" + k).value.trim();
        if (!v) { toast("Paste a key first"); return; }
        var sec = {}; sec[k] = v; saveSettings({ secrets: sec }, "Key saved — checking now");
      });
    });
    el.querySelectorAll("[data-key-del]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (!confirm("Remove this key from the Pi?")) return;
        var sec = {}; sec[b.getAttribute("data-key-del")] = ""; saveSettings({ secrets: sec }, "Key removed");
      });
    });
    $("#ms-save").addEventListener("click", function () { saveSettings({ moonshot_base_url: $("#ms-base").value }); });
    $("#sub-add").addEventListener("click", function () { $("#subs-table tbody").insertAdjacentHTML("beforeend", subRow()); bindDel(); });
    $("#man-add").addEventListener("click", function () { $("#man-table tbody").insertAdjacentHTML("beforeend", manRow()); bindDel(); });
    $("#subs-save").addEventListener("click", function () { saveSettings({ subscriptions: readRows("#subs-table") }, "Subscriptions saved"); });
    $("#man-save").addEventListener("click", function () { saveSettings({ manual_entries: readRows("#man-table") }, "Entries saved"); });
    $("#poll-save").addEventListener("click", function () {
      var iv = {}, en = {};
      el.querySelectorAll("[data-iv]").forEach(function (i) { iv[i.getAttribute("data-iv")] = parseInt(i.value, 10); });
      el.querySelectorAll("[data-en]").forEach(function (i) { en[i.getAttribute("data-en")] = i.checked; });
      saveSettings({ intervals: iv, enabled: en });
    });
    $("#nw-save").addEventListener("click", function () {
      var split = function (v) { return v.split(",").map(function (s) { return s.trim(); }).filter(Boolean); };
      saveSettings({ news_watch: { families: split($("#nw-fam").value), hn_queries: split($("#nw-hn").value) } }, "Watch list saved — refreshing news");
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

  // ------------------------------------------------------------------ news & models
  var N = { data: null, filter: "you", valueTable: false };
  var FILTERS = [["you", "For you"], ["release", "Releases"], ["vendor", "Vendors"], ["community", "Community"], ["stealth", "Stealth chatter"], ["all", "All"]];

  function usd(v) {
    if (v == null) return "—";
    if (v === 0) return "free";
    return "$" + (v < 0.1 ? v.toFixed(3) : v < 10 ? v.toFixed(2) : v.toFixed(0));
  }
  function day(ts) { return ts ? new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short" }) : ""; }
  function star(m) { return m.watched ? '<span class="you" title="A family you use">★</span>' : ""; }

  function loadNews() {
    var v = $("#view-news");
    if (N.data) v.classList.add("loading");
    return api("/api/news").then(function (d) { N.data = d; renderNews(); })
      .catch(function (e) { $("#news-errors").textContent = "Couldn't load news: " + e.message; })
      .then(function () { v.classList.remove("loading"); });
  }

  function renderNews() {
    var d = N.data, m = d.models || {};
    var up = Math.max(d.updated.feeds || 0, d.updated.catalog || 0);
    $("#news-updated").textContent = up ? "updated " + ago(up) : "first fetch runs a minute after start-up…";
    $("#news-watch").innerHTML = "Watching: " + esc((d.watch.families || []).join(", ")) + ' · <a href="#/settings">change</a>';

    // Stealth
    var st = m.stealth || [], gone = m.stealth_gone || [];
    var chatter = d.items.filter(function (i) { return i.group === "stealth"; }).slice(0, 4);
    var h = st.length ? '<ul class="mlist">' + st.map(function (x) {
      return "<li><div><b>" + esc(x.name) + '</b><div class="mid">' + esc(x.id) + " · " + esc(x.desc || "") + '</div></div><div class="meta">' +
        usd(x.blended) + (x.ctx ? "<br>" + Math.round(x.ctx / 1000) + "k ctx" : "") + "<br>since " + day(x.first_seen) + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">No stealth models live on OpenRouter right now — this card lights up when one appears.</div>';
    if (gone.length) h += '<div class="muted small" style="margin-top:8px">Previously seen: ' + gone.map(function (g) { return esc(g.name || g.id) + " (" + day(g.first_seen) + "–" + day(g.last_seen) + ")"; }).join(", ") + "</div>";
    if (chatter.length) h += '<h3 class="small" style="margin:12px 0 4px">Stealth chatter</h3><ul class="mlist">' + chatter.map(function (i) {
      return '<li><a href="' + esc(i.url) + '" target="_blank" rel="noopener noreferrer">' + esc(i.title) + '</a><span class="meta">' + day(i.ts) + "</span></li>";
    }).join("") + "</ul>";
    $("#n-stealth").innerHTML = h;

    // New models
    var nm = m.new || [];
    $("#n-new").innerHTML = nm.length ? '<ul class="mlist">' + nm.slice(0, 10).map(function (x) {
      return "<li><div>" + star(x) + "<b>" + esc(x.name) + '</b><div class="mid">' + esc(x.id) + (x.q ? " · quality " + x.q : "") + '</div></div><div class="meta">' +
        day(x.created) + "<br>" + usd(x.blended) + "/M" + (x.ctx ? " · " + Math.round(x.ctx / 1000) + "k" : "") + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">Waiting for the first catalogue fetch…</div>';

    renderValue(m);

    // Retiring
    var rt = m.retiring || [];
    $("#n-retiring").innerHTML = rt.length ? '<ul class="mlist">' + rt.slice(0, 8).map(function (x) {
      var days = Math.round((x.exp_ts - Date.now() / 1000) / 86400);
      return "<li><div>" + star(x) + esc(x.name) + '<div class="mid">' + esc(x.id) + '</div></div><div class="meta">' + esc(x.exp) + "<br>" +
        (days <= 7 ? '<span class="warnline">⚠ ' + Math.max(0, days) + " days</span>" : days + " days") + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">Nothing scheduled for retirement.</div>';

    // Price changes
    var pc = m.price_changes || [];
    $("#n-prices").innerHTML = pc.length ? '<ul class="mlist">' + pc.slice(0, 8).map(function (c) {
      var down = c.pct < 0;
      return "<li><div>" + star(c) + esc(c.name) + '<div class="mid">' + esc(c.id) + '</div></div><div class="meta">' + usd(c.old) + " → " + usd(c.new) + "/M<br>" +
        '<span style="color:var(--' + (down ? "good-text" : "critical-text") + ')">' + (down ? "▼ " : "▲ ") + Math.abs(c.pct) + "%</span> · " + day(c.ts) + "</div></li>";
    }).join("") + "</ul>" : '<div class="empty-note">No price changes seen yet — the Pi compares the catalogue day to day.</div>';

    // Feed
    $("#n-filters").innerHTML = FILTERS.map(function (f) {
      return '<button class="chip" data-f="' + f[0] + '" aria-pressed="' + (N.filter === f[0]) + '">' + f[1] + "</button>";
    }).join("");
    $("#n-filters").querySelectorAll(".chip").forEach(function (b) {
      b.addEventListener("click", function () { N.filter = b.getAttribute("data-f"); save("newsFilter", N.filter); renderNews(); });
    });
    var items = d.items.filter(function (i) {
      if (N.filter === "all") return true;
      if (N.filter === "you") return i.relevant && i.group !== "stealth";
      return i.group === N.filter;
    }).slice(0, 40);
    var GROUP = { release: "release", vendor: "news", community: "community", stealth: "stealth" };
    $("#n-feed").innerHTML = items.length ? items.map(function (i) {
      return '<div class="news-item"><a class="t" href="' + esc(i.url) + '" target="_blank" rel="noopener noreferrer">' + esc(i.title) + "</a>" +
        '<div class="src"><span class="tag">' + esc(GROUP[i.group] || i.group) + "</span>" + esc(i.source) + " · " + day(i.ts) +
        (i.discuss && i.discuss !== i.url ? ' · <a href="' + esc(i.discuss) + '" target="_blank" rel="noopener noreferrer">discuss</a>' : "") + "</div>" +
        (i.summary ? '<div class="sum">' + esc(i.summary) + "</div>" : "") + "</div>";
    }).join("") : '<div class="empty-note">Nothing here yet.</div>';

    var errs = Object.keys(d.errors || {});
    $("#news-errors").textContent = errs.length ? "Some sources failed last time: " + errs.map(function (k) { return k + " (" + d.errors[k] + ")"; }).join("; ") : "";
  }

  function renderValue(m) {
    var el = $("#n-value"), tiers = m.value_tiers || [], frontier = m.frontier || [];
    var btn = $("#bb-toggle");
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
    // Scatter: every rated model in the tiers + frontier; x = log price, y = Elo.
    var pts = {}, add = function (x, onF) {
      if (!x.blended || x.blended <= 0 || !x.q) return;
      var k = x.id; pts[k] = pts[k] || { id: x.id, name: x.name, q: x.q, p: x.blended, you: !!x.watched, f: false };
      if (onF) pts[k].f = true;
    };
    tiers.forEach(function (t) { t.models.forEach(function (x) { add(x, false); }); });
    frontier.forEach(function (x) { add(x, true); });
    var list = Object.keys(pts).map(function (k) { return pts[k]; });
    // Fit the y-axis to the competitive range: far-off outliers stay in the table view, not the chart.
    var topQ = Math.max.apply(null, list.map(function (p) { return p.q; }));
    var hidden = list.filter(function (p) { return p.q < topQ - 250; }).length;
    list = list.filter(function (p) { return p.q >= topQ - 250; });
    var W = 760, H = 300, L = 46, R = 12, T = 12, B = 30;
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
      var tip = "<b>" + esc(p.name) + "</b><br>Quality " + p.q + " · " + usd(p.p) + " per 1M" + (p.f ? "<br>On the frontier" : "") + (p.you ? "<br>★ a family you use" : "");
      marks += '<g tabindex="0" data-tip="' + tip.replace(/"/g, "&quot;") + '"><circle cx="' + cx + '" cy="' + cy + '" r="12" fill="transparent"/>' +
        '<circle cx="' + cx + '" cy="' + cy + '" r="5" fill="var(--' + (p.you ? "ink" : "axis") + ')" stroke="var(--surface)" stroke-width="2"/></g>';
    });
    // Direct-label frontier points only, greedily skipping any label that would collide (tooltip covers the rest).
    fl.slice().reverse().forEach(function (p) {
      var text = p.name.replace(/^[^:]+:\s*/, ""), w = text.length * 6.2, cx = sx(p.p), cy = sy(p.q);
      var cands = [[cx + 8, cy - 8], [cx + 8, cy + 16], [cx - 8 - w, cy - 8], [cx - 8 - w, cy + 16]];
      for (var i = 0; i < cands.length; i++) {
        var x = cands[i][0], y = cands[i][1];
        if (x < L || x + w > W - R || y < T + 8 || y > H - B - 2) continue;
        var clash = labels.some(function (b) { return x < b.x + b.w + 4 && b.x < x + w + 4 && Math.abs(y - b.y) < 13; });
        if (!clash) { labels.push({ x: x, y: y, w: w }); svg += '<text x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" font-size="11" fill="var(--ink-2)" stroke="var(--surface)" stroke-width="4" paint-order="stroke" stroke-linejoin="round">' + esc(text) + "</text>"; break; }
      }
    });
    svg += marks + "</svg>";
    el.innerHTML = '<div class="value-chart">' + svg + "</div>" +
      '<div class="muted small inline" style="gap:16px;margin-top:4px"><span><span class="swatch" style="background:var(--ink);border-radius:50%"></span>families you use</span>' +
      '<span><span class="swatch" style="background:var(--axis);border-radius:50%"></span>others</span>' +
      '<span><span class="swatch" style="background:var(--ink-2);height:2px;vertical-align:3px"></span>frontier</span>' +
      (hidden ? "<span>" + hidden + " far-cheaper, much weaker model" + (hidden > 1 ? "s" : "") + " in the table view</span>" : "") + "</div>" + tierHtml;
    bindTips(el);
  }

  // ------------------------------------------------------------------ routing + boot
  function route() {
    var view = location.hash === "#/settings" ? "settings" : location.hash === "#/news" ? "news" : "dash";
    $("#view-dash").hidden = view !== "dash";
    $("#view-news").hidden = view !== "news";
    $("#view-settings").hidden = view !== "settings";
    $("#nav-dash").hidden = view === "dash";
    $("#nav-news").hidden = view === "news";
    $("#nav-settings").hidden = view === "settings";
    $(".filters").hidden = view !== "dash";
    if (view === "settings") loadSettings();
    if (view === "news") loadNews();
    hideTip();
  }

  S.credits = load("credits", "used");
  N.filter = load("newsFilter", "you");
  $("#bb-toggle").addEventListener("click", function () { N.valueTable = !N.valueTable; if (N.data) renderValue(N.data.models || {}); });
  setInterval(function () { if (!document.hidden && location.hash === "#/news") loadNews(); }, 5 * 60000);
  $("#f-month").addEventListener("change", function (e) { S.month = e.target.value; refresh(); });
  $("#f-currency").addEventListener("change", function (e) {
    api("/api/currency", { currency: e.target.value }).then(refresh).catch(function (err) { toast(err.message); });
  });
  $("#f-credits").addEventListener("change", function (e) { S.credits = e.target.value; save("credits", S.credits); render(); });
  $("#tr-toggle").addEventListener("click", function () { S.trendTable = !S.trendTable; renderTrend(S.state); });
  window.addEventListener("hashchange", route);
  route();
  refresh();
  setInterval(function () { if (!document.hidden && (location.hash === "" || location.hash === "#/")) refresh(); }, REFRESH_MS);
})();
