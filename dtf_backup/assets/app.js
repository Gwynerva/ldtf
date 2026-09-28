(function () {
  "use strict";
  var root = document.documentElement;

  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  // icons rendered by the server into <template id="icons"> / <template class="icons">
  function ic(n) {
    var ts = document.querySelectorAll("template#icons, template.icons");
    for (var i = 0; i < ts.length; i++) { var el = ts[i].content.querySelector('[data-n="' + n + '"]'); if (el) return el.innerHTML; }
    return "";
  }
  function appbarH() { return parseInt(getComputedStyle(root).getPropertyValue("--appbar-h"), 10) || 72; }

  // ---- theme (the initial value is applied by an inline script in <head>)
  var themeMeta = document.querySelector('meta[name="theme-color"]');
  function syncThemeColor() { if (themeMeta) themeMeta.content = getComputedStyle(document.body).backgroundColor; }
  syncThemeColor();
  document.addEventListener("click", function (e) {
    if (!e.target.closest || !e.target.closest(".theme-btn")) return;
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next); store("dtf-theme", next); syncThemeColor();
  });

  // ---- app bar: a stronger shadow once the page is scrolled
  var ticking = false;
  function onScroll() { root.classList.toggle("scrolled", window.scrollY > 4); ticking = false; }
  window.addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();

  // ---- menus on <details>: close on outside click / Esc
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.acct[open], details.menu[open]").forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll("details.acct[open], details.menu[open]").forEach(function (d) { d.open = false; });
  });

  // ---- comment threads: the line / "Свернуть" collapse a branch, "Развернуть ветку" opens it again
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var line = e.target.closest(".c-line"), more = e.target.closest(".c-more"), tg = e.target.closest(".c-toggle");
    var btn = line || more || tg;
    if (!btn) return;
    var node = line ? line.parentNode.closest(".c-node") : btn.closest(".c-node");
    var collapse = line ? true : more ? false : !node.classList.contains("collapsed");
    node.classList.toggle("collapsed", collapse);
    var t = node.querySelector(":scope > .c .c-toggle");
    if (t) t.textContent = collapse ? t.getAttribute("data-open") : t.getAttribute("data-close");
    if (collapse) { var r = node.getBoundingClientRect(), top = appbarH(); if (r.top < top) window.scrollBy(0, r.top - top - 8); }
  });

  // ---- gif-like videos: play only while visible
  var io = "IntersectionObserver" in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      var v = en.target;
      if (en.isIntersecting) { var p = v.play(); if (p && p.catch) p.catch(function () {}); } else v.pause();
    });
  }, { rootMargin: "200px" }) : null;
  function observe(el) {
    el.querySelectorAll("video[data-autoplay]").forEach(function (v) { if (io) io.observe(v); else v.autoplay = true; });
  }
  observe(document);

  // ---- "Продолжить ветку": a deep branch in a focus view where indentation starts from zero
  var stack = [];
  function strip(el) { el.removeAttribute("id"); el.querySelectorAll("[id]").forEach(function (x) { x.removeAttribute("id"); }); return el; }
  function wrapNode(c, cls) { var d = document.createElement("div"); d.className = "c-node " + (cls || ""); d.appendChild(strip(c.cloneNode(true))); return d; }
  function ancestorsOf(node) {
    var out = [], p = node.parentElement && node.parentElement.closest(".c-node");
    while (p) { out.unshift(p.querySelector(":scope > .c")); p = p.parentElement && p.parentElement.closest(".c-node"); }
    var art = node.closest("article.mc");
    if (art) {  // month pages: the flat context chain above the user's comment
      var flat = art.querySelectorAll(":scope > details.ctx-more .c, :scope > .c-node.ctx > .c");
      out = Array.prototype.slice.call(flat).concat(out);
    }
    return out.filter(Boolean);
  }
  function render(node) {
    var ov = document.getElementById("focus");
    if (!ov) { ov = document.createElement("div"); ov.id = "focus"; ov.className = "focus"; document.body.appendChild(ov); }
    ov.innerHTML = '<div class="focus-in"><div class="focus-bar"><button class="icon-btn focus-back" type="button" title="Назад (Esc)" ' +
      'aria-label="Назад">' + ic("arrow_back") + '</button><span class="focus-title">Продолжение ветки</span>' +
      '<span class="muted small">уровень ' + stack.length + '</span></div></div>';
    var inn = ov.firstChild, art = node.closest("article.mc"), anc = ancestorsOf(node);
    if (art && art.querySelector(".mc-post")) inn.appendChild(strip(art.querySelector(".mc-post").cloneNode(true)));
    if (anc.length > 1) {
      var det = document.createElement("details"); det.className = "focus-anc";
      det.innerHTML = "<summary>Выше по ветке: ещё " + (anc.length - 1) + "</summary>";
      anc.slice(0, -1).forEach(function (c) { det.appendChild(wrapNode(c, "focus-ctx")); });
      inn.appendChild(det);
    }
    if (anc.length) inn.appendChild(wrapNode(anc[anc.length - 1], "focus-ctx"));
    var sub = strip(node.cloneNode(true)); sub.classList.remove("collapsed"); sub.classList.add("focus-root");
    sub.__orig = node; inn.appendChild(sub);
    document.body.classList.add("focus-open"); ov.hidden = false; ov.scrollTop = 0; observe(ov);
  }
  function closeFocus() { var ov = document.getElementById("focus"); if (ov) { ov.hidden = true; ov.innerHTML = ""; } document.body.classList.remove("focus-open"); }
  var viaHistory = [];  // per level: did pushState work?
  function openFocus(node) {
    stack.push(node); render(node);
    var ok = true;
    try { history.pushState({ focus: stack.length }, "", "#ветка-" + stack.length); } catch (e) { ok = false; }
    viaHistory.push(ok);
  }
  function goBack() { if (viaHistory[viaHistory.length - 1]) history.back(); else back(); }
  function back() { stack.pop(); viaHistory.pop(); if (stack.length) render(stack[stack.length - 1]); else closeFocus(); }
  window.addEventListener("popstate", function () { if (stack.length) back(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && stack.length && !document.querySelector(".pswp--open")) goBack(); });
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    if (e.target.closest(".focus-back")) { goBack(); return; }
    var btn = e.target.closest(".c-cont"); if (!btn) return;
    var node = btn.closest(".c-node");
    var rootNode = node.closest(".focus-root");
    if (rootNode && rootNode.__orig) {  // map a node inside the focus view back to the page, keeping real ancestors
      var id = node.querySelector(":scope > .c").getAttribute("data-id");
      var orig = rootNode.__orig.querySelector('.c[data-id="' + id + '"]');
      if (orig) node = orig.closest(".c-node");
    }
    openFocus(node);
  });

  // ---- PhotoSwipe lightbox for every picture (and gif-video); one gallery per .pswp-gallery container.
  // Click delegation (not lightbox.init binding) so it also works inside the cloned "continue thread" view.
  var lb = null, openGallery = null;
  if (window.PhotoSwipeLightbox && window.PhotoSwipe) {
    lb = new window.PhotoSwipeLightbox({
      pswpModule: window.PhotoSwipe, bgOpacity: 0.94, wheelToZoom: true, preload: [1, 2],
      closeTitle: "Закрыть (Esc)", zoomTitle: "Масштаб (Z)", arrowPrevTitle: "Назад (←)", arrowNextTitle: "Вперёд (→)",
      errorMsg: "Не удалось загрузить изображение", indexIndicatorSep: " из "
    });
    lb.on("change", function () { if (openGallery && lb.pswp) selectSlide(openGallery, lb.pswp.currIndex); });
    lb.on("destroy", function () { openGallery = null; });
    lb.init();
  }
  function pswpItem(a) {
    var img = a.querySelector("img"), vid = a.querySelector("video");
    var w = +a.getAttribute("data-pswp-width") || (img && img.naturalWidth) || (vid && vid.videoWidth) || 1600;
    var h = +a.getAttribute("data-pswp-height") || (img && img.naturalHeight) || (vid && vid.videoHeight) || 1200;
    if (a.hasAttribute("data-pswp-video")) {
      return { html: '<div class="pswp-video"><video src="' + a.href.replace(/"/g, "%22") +
        '" autoplay loop muted playsinline controls></video></div>', element: a, width: w, height: h };
    }
    return { src: a.href, msrc: img ? (img.currentSrc || img.src) : undefined, width: w, height: h, element: a };
  }
  function selectSlide(g, i) {
    g.querySelectorAll(".g-tile").forEach(function (t) { t.classList.toggle("on", +t.getAttribute("data-i") === i); });
    g.querySelectorAll(".g-slide").forEach(function (s) {
      var on = +s.getAttribute("data-i") === i;
      s.classList.toggle("on", on);
      if (!on) s.querySelectorAll("video").forEach(function (v) { v.pause(); });
    });
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var tile = e.target.closest(".g-tile");
    if (tile) { selectSlide(tile.closest(".b-gallery"), +tile.getAttribute("data-i")); return; }
    var a = e.target.closest("a.pswp-item");
    if (!a || !lb || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
    var g = a.closest(".pswp-gallery");
    var items = g ? Array.prototype.slice.call(g.querySelectorAll("a.pswp-item")) : [a];
    e.preventDefault();
    openGallery = g && g.classList.contains("b-gallery") ? g : null;
    lb.loadAndOpen(Math.max(0, items.indexOf(a)), items.map(pswpItem));
  });

  // ---- month pages: filter chips
  document.querySelectorAll(".filters input[data-f]").forEach(function (cb) {
    cb.addEventListener("change", function () { document.body.classList.toggle("f-" + cb.getAttribute("data-f"), cb.checked); });
  });

  // ---- posts: sorting moves the same cards between the year groups and one flat list
  var sortBtns = document.querySelectorAll(".sortbar button[data-sort]");
  var byYear = document.getElementById("by-year"), flatList = document.getElementById("flat"), homes = null;
  sortBtns.forEach(function (b) {
    b.addEventListener("click", function () {
      sortBtns.forEach(function (x) { x.classList.toggle("on", x === b); });
      var key = b.getAttribute("data-sort");
      if (!homes) homes = Array.prototype.map.call(byYear.querySelectorAll(".pcard"), function (el) { return { el: el, parent: el.parentNode }; });
      if (key === "date") {
        homes.forEach(function (h) { h.parent.appendChild(h.el); });
      } else {
        homes.map(function (h) { return h.el; })
          .sort(function (a, c) { return (+c.getAttribute("data-" + key)) - (+a.getAttribute("data-" + key)); })
          .forEach(function (c) { flatList.appendChild(c); });
      }
      byYear.hidden = key !== "date"; flatList.hidden = key === "date";
    });
  });

  // ---- search: filters apply immediately
  document.querySelectorAll("form[data-autosubmit]").forEach(function (f) {
    f.addEventListener("change", function (e) {
      var q = f.querySelector('input[name="q"]');
      if (e.target.matches("select, input[type=checkbox]") && q && q.value.trim()) f.submit();
    });
  });

  // ---- forms that need an explicit confirmation (deleting an archive)
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // ---- snackbars fade out by themselves
  document.querySelectorAll(".snackbar").forEach(function (s) {
    setTimeout(function () { s.classList.add("out"); setTimeout(function () { s.remove(); }, 400); }, 4000);
  });

  // ---- sync jobs: progress ring in the app bar, marks in the archive menu, the panel on /u/<nick>/sync
  function fmtN(n) { return String(Math.round(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0"); }
  function fmtB(b) { var u = ["Б", "КБ", "МБ", "ГБ", "ТБ"], i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return (i ? b.toFixed(1) : b) + " " + u[i]; }
  function fmtT(s) { s = Math.max(0, Math.round(s)); var m = Math.floor(s / 60), h = Math.floor(m / 60);
    return h ? h + " ч " + (m % 60) + " мин" : m ? m + " мин " + (s % 60) + " с" : s + " с"; }
  function running(j) { return j.stages.filter(function (s) { return s.status === "running"; })[0]; }
  function overall(j) {  // share of the whole job: finished stages + the running one's percent
    var n = j.stages.length || 1, done = 0;
    j.stages.forEach(function (s) {
      if (s.status === "done" || s.status === "skipped") done += 1;
      else if (s.status === "running" && s.pct != null) done += s.pct / 100;
    });
    return Math.min(100, Math.round(100 * done / n));
  }
  var ind = document.querySelector(".job-ind");
  function pollIndicator() {
    if (document.hidden) return;
    fetch("/api/jobs").then(function (r) { return r.json(); }).then(function (list) {
      var busy = {};
      list.forEach(function (j) { busy[j.nick] = true; });
      document.querySelectorAll(".acct-item[data-nick]").forEach(function (a) {
        var s = a.querySelector(".acct-sync"); if (s) s.hidden = !busy[a.getAttribute("data-nick")];
      });
      var j = list.filter(function (x) { return x.state === "running"; })[0] || list[0];
      if (!ind) return;
      if (!j) { ind.hidden = true; return; }
      var s = running(j), pct = overall(j), queued = j.state === "queued";
      ind.hidden = false;
      ind.href = "/u/" + encodeURIComponent(j.nick) + "/sync";
      ind.classList.toggle("ind", queued || !s);
      var bar = ind.querySelector(".rb");
      if (bar) bar.style.strokeDashoffset = String(56.55 * (1 - (queued ? 0.25 : pct / 100)));
      ind.querySelector(".job-pct").textContent = queued ? "" : pct + "%";
      ind.title = "@" + j.nick + ": " + (queued ? "в очереди" : s ? s.title + (s.pct != null ? " " + Math.round(s.pct) + "%" : "") : "синхронизация") +
        " — открыть";
    }).catch(function () {});
  }
  if (ind) { pollIndicator(); setInterval(pollIndicator, 4000); document.addEventListener("visibilitychange", pollIndicator); }

  var jp = document.getElementById("job-panel");
  if (jp && jp.getAttribute("data-job")) {
    var jobId = jp.getAttribute("data-job");
    var stagesEl = jp.querySelector(".jp-stages"), logEl = jp.querySelector(".jp-log"), logW = jp.querySelector(".jp-logw");
    var stateEl = jp.querySelector(".jp-state"), timeEl = jp.querySelector(".jp-time"), icEl = jp.querySelector(".jp-ic");
    var stopF = jp.querySelector(".job-stop"), acts = jp.querySelector(".job-actions");
    var STATE = { queued: "В очереди", running: "Идёт синхронизация", done: "Готово",
                  cancelled: "Остановлено — прогресс сохранён", error: "Ошибка",
                  blocked: "Остановлено защитой архива" };
    var JIC = { queued: "schedule", running: "sync", done: "check_circle", cancelled: "cancel", error: "error",
                blocked: "gpp_maybe" };
    var ST = { pending: "ожидает", running: "идёт", done: "готово", skipped: "пропущено", stopped: "остановлено", error: "ошибка" };
    var SIC = { pending: "radio_button_unchecked", running: "progress_activity", done: "check_circle", skipped: "block",
                stopped: "cancel", error: "error" };
    function details(s) {
      var p = [];
      if (s.key === "comments" && s.comments != null) {
        p.push(fmtN(s.comments) + " комментариев");
        if (s.phase === "backfill" && s.total) p.push("месяцев " + s.done + " из " + s.total);
        if (s.phase === "head") p.push("проверка свежих, стр. " + (s.pages || 0));
      }
      if (s.key === "posts" && s.listed != null) p.push("в профиле " + s.listed);
      if (s.key === "media") {
        if (s.bytes) p.push("скачано " + fmtB(s.bytes));
        if (s.shared) p.push("уже в хранилище " + fmtN(s.shared));
      }
      if (s.errors) p.push("ошибок " + s.errors);
      if (s.rate && s.key !== "build") p.push(s.rate.toFixed(1) + "/с");
      if (s.eta) p.push("осталось ≈ " + fmtT(s.eta));
      if (s.phaseTitle) p.push(s.phaseTitle);
      return p.join(" · ");
    }
    function render(j) {
      var live = j.state === "queued" || j.state === "running";
      stateEl.textContent = STATE[j.state] || j.state;
      icEl.className = "jp-ic " + j.state;
      icEl.innerHTML = ic(JIC[j.state] || "sync");
      var t0 = j.started || j.created, t1 = j.finished || Date.now() / 1000;
      timeEl.textContent = (j.params && j.params.full ? "полная перепроверка · " : "") + (t0 ? "время " + fmtT(t1 - t0) : "");
      var html = j.stages.map(function (s) {
        if (!live && s.status === "running") s = Object.assign({}, s, { status: j.state === "error" ? "error" : "stopped", eta: null });
        var run = s.status === "running";
        var pct = s.status === "done" ? 100 : s.pct != null ? s.pct : null;
        var val = s.total && s.done != null ? fmtN(s.done) + " / " + fmtN(s.total) : ST[s.status] || s.status;
        if (run && pct != null) val += " · " + Math.round(pct) + "%";
        var bar = !run ? "" : pct != null ? '<div class="lp"><div class="lp-i" style="width:' + pct + '%"></div></div>'
                                          : '<div class="lp ind"><div class="lp-i"></div></div>';
        var d = run || s.status === "error" ? details(s) : "";
        return '<div class="st st-' + s.status + '"><div class="st-h">' + ic(SIC[s.status] || "radio_button_unchecked") +
          '<span class="st-t">' + esc(s.title) + '</span><span class="st-v">' + esc(val) + "</span></div>" + bar +
          (d ? '<div class="st-d">' + esc(d) + "</div>" : "") + "</div>";
      }).join("") + (j.error ? '<div class="banner err jp-err">' + ic("error") + '<div class="banner-t">' + esc(j.error) + "</div></div>" : "");
      if (stagesEl.__html !== html) { stagesEl.innerHTML = html; stagesEl.__html = html; }
      var txt = j.log.join("\n");
      if (logEl.textContent !== txt) {  // keep the reader's scroll position; follow the tail only when at the bottom
        var pos = logEl.scrollTop, atEnd = pos + logEl.clientHeight >= logEl.scrollHeight - 20;
        logEl.textContent = txt;
        logEl.scrollTop = atEnd ? logEl.scrollHeight : pos;
      }
      if (logW) logW.hidden = false;
      if (stopF) stopF.hidden = !live;
      if (acts) acts.hidden = live;
      jp.setAttribute("data-running", live ? "1" : "0");
    }
    var seenLive = false;
    function refreshStatus() {  // the job finished while we watched: update the numbers in place, no reload
      fetch(location.pathname).then(function (r) { return r.text(); }).then(function (t) {
        var doc = new DOMParser().parseFromString(t, "text/html");
        [["pre.status", "textContent"], [".sync-stats", "innerHTML"]].forEach(function (p) {
          var fresh = doc.querySelector(p[0]), cur = document.querySelector(p[0]);
          if (fresh && cur) cur[p[1]] = fresh[p[1]];
        });
      }).catch(function () {});
    }
    function poll() {
      fetch("/api/jobs/" + jobId).then(function (r) { return r.json(); }).then(function (j) {
        if (j.error && !j.stages) return;
        render(j);
        if (j.state === "queued" || j.state === "running") { seenLive = true; setTimeout(poll, 1000); }
        else if (seenLive) { seenLive = false; if (j.state === "blocked") location.reload(); else refreshStatus(); }
      }).catch(function () { setTimeout(poll, 3000); });
    }
    poll();
  }

  // ---- diagnostics: refresh while checks are running
  if (document.querySelector('.diag[data-running="1"]')) setTimeout(function () { location.reload(); }, 2000);

  // ---- Direct links (#c123 from search/calendar, #anchor from a table of contents): reveal + temporary highlight.
  function flash(el) {
    if (!el) return;
    el.classList.remove("hl"); void el.offsetWidth; el.classList.add("hl");
    clearTimeout(el.__hlTimer);
    el.__hlTimer = setTimeout(function () { el.classList.remove("hl"); }, 2700);
  }
  function hidden(el) { return el.offsetParent === null && getComputedStyle(el).position !== "fixed"; }
  function revealComment(c) {
    // expand collapsed branches and folded context around it
    for (var n = c.closest(".c-node"); n; n = n.parentElement && n.parentElement.closest(".c-node")) n.classList.remove("collapsed");
    for (var d = c.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) d.open = true;
    // deeper than the indentation limit: open the "continue thread" view in ONE step, rooted two levels above
    // the target (visible within the limit on any screen); everything higher is under "Выше по ветке".
    if (hidden(c) && !c.closest("#focus")) {
      var chain = [];
      for (var q = c.closest(".c-node"); q; q = q.parentElement && q.parentElement.closest(".c-node")) chain.push(q);
      var host = chain[Math.min(2, chain.length - 1)];
      if (host) {
        openFocus(host);
        var copy = document.querySelector('#focus .focus-root .c[data-id="' + c.getAttribute("data-id") + '"]');
        if (copy) c = copy;
      }
    }
    return c;
  }
  function revealTarget(hash) {
    if (!hash || hash.length < 2) return;
    var id; try { id = decodeURIComponent(hash.slice(1)); } catch (e) { id = hash.slice(1); }
    var el = document.getElementById(id);
    if (!el) return;
    var target = el.classList.contains("c") ? revealComment(el) : el;
    var sp = el.closest("details.spoiler"); if (sp) sp.open = true;
    setTimeout(function () {
      target.scrollIntoView({ block: target.offsetHeight > innerHeight * 0.6 ? "start" : "center", behavior: "smooth" });
      flash(target);
    }, 60);
  }
  window.addEventListener("hashchange", function () { if (!/^#ветка-/.test(decodeURIComponent(location.hash))) revealTarget(location.hash); });
  if (location.hash && !/^#ветка-/.test(decodeURIComponent(location.hash))) {
    if (document.readyState === "complete") revealTarget(location.hash);
    else window.addEventListener("load", function () { revealTarget(location.hash); });
  }
})();
