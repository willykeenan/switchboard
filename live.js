/* Agents, live: every team in a stable structural order. Watching is passive; actions go through the board. */
(function () {
  "use strict";
  var filter = "active", onlyProject = "", query = "", timer = null, last = null, failures = 0;
  var rows = new Map(), panels = new Map(), headings = new Map();
  var ACTIVE = { "needs-you": 1, working: 1, recent: 1 };
  var PHASE_WORDS = { tools: "Using a tool…", waiting: "Waiting on a tool or timer…", quiet: "No recent update",
    unknown: "Idle", unavailable: "Log not readable" };
  var STUCK_SECONDS = 300;
  var collapsed = {};
  try { collapsed = JSON.parse(localStorage.getItem("live.collapsed") || "{}") || {}; } catch (e) { collapsed = {}; }
  var $ = function (id) { return document.getElementById(id); };

  function epoch(v) { var t = v ? Date.parse(v) : NaN; return isNaN(t) ? null : t; }
  function ago(ms) {
    if (ms === null) return "";
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function span(sec) {
    if (typeof sec !== "number" || !isFinite(sec)) return "";
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + "s";
    var m = Math.floor(sec / 60);
    if (m < 60) return m + "m";
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function stuck(s) {
    var a = s.activity || {};
    return a.turnStatus === "open" && a.phase === "quiet" && typeof a.ageSeconds === "number" && a.ageSeconds >= STUCK_SECONDS;
  }

  function stateText(s) {
    var a = s.activity || {};
    if (stuck(s)) return "No update for " + span(a.ageSeconds);
    return PHASE_WORDS[a.phase] || a.label || "Activity unknown";
  }

  function row(s) {
    var r = rows.get(s.id);
    if (!r) {
      r = el("div", "row");
      r.tabIndex = 0;
      r.setAttribute("role", "button");
      r.addEventListener("click", function () { r.classList.toggle("open"); r.setAttribute("aria-expanded", r.classList.contains("open")); });
      r.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); r.click(); } });
      rows.set(s.id, r);
    }
    var a = s.activity || {};
    r.dataset.bucket = s.bucket;
    r.dataset.phase = a.phase || "unknown";
    r.classList.toggle("stuck", stuck(s));
    r.setAttribute("aria-expanded", r.classList.contains("open"));
    if (r.dataset.editing === "1") return r;
    var seenAt = epoch(a.observedAt) || epoch(s.lastSeenAt);
    var turn = a.active && typeof a.durationSeconds === "number" ? "turn " + span(a.durationSeconds) :
      (typeof a.lastFinishedDurationSeconds === "number" && s.bucket === "recent" ? "last " + span(a.lastFinishedDurationSeconds) : "");
    var sig = JSON.stringify([s.title, s.team, s.provider, s.bucket, a.phase, stateText(s), turn, s.workingOn, s.waitingOn, a.publicAction,
      a.model, a.reasoning, a.turnStatus, a.lastFinishedAt, s.note, s.managed, s.endpoint]);
    if (r.dataset.sig === sig) {
      var u = r.querySelector(".age");
      if (u) u.textContent = seenAt ? ago(seenAt) : "";
      return r;
    }
    r.dataset.sig = sig;
    while (r.firstChild) r.removeChild(r.firstChild);

    r.appendChild(el("span", "pip"));
    var who = el("div", "who");
    who.appendChild(el("div", "name", s.title));
    who.appendChild(el("div", "team", [s.team, s.provider === "claude" ? "Claude Code" : s.provider === "codex" ? "Codex" : s.provider].filter(Boolean).join(" · ")));
    r.appendChild(who);

    var now = el("div", "now");
    now.appendChild(el("span", "label", stateText(s)));
    now.appendChild(el("span", "age", seenAt ? ago(seenAt) : ""));
    r.appendChild(now);

    var on = el("div", "on");
    if (s.workingOn) {
      var line = el("div");
      line.appendChild(document.createTextNode("On: "));
      line.appendChild(el("span", "what", s.workingOn.text));
      if (s.workingOn.from) line.appendChild(el("span", "from", " · from " + s.workingOn.from));
      line.title = s.workingOn.text + (s.workingOn.from ? " (from " + s.workingOn.from + ")" : "");
      on.appendChild(line);
    }
    if (s.waitingOn && s.waitingOn.length) {
      on.appendChild(el("div", "wait", "Waiting on " + s.waitingOn.join(", ")));
    }
    if (a.publicAction && !(s.workingOn)) on.appendChild(el("div", "what", a.publicAction));
    r.appendChild(on);
    r.appendChild(el("div", "turn", turn));

    var more = el("div", "more");
    if (a.publicAction) more.appendChild(el("div", null, "Last said: " + a.publicAction));
    if (a.lastAction) more.appendChild(el("div", null, "Last step: " + a.lastAction));
    [["Model", [a.model, a.reasoning].filter(Boolean).join(" · ")], ["Session", s.id],
     ["Last finished", a.lastFinishedAt ? new Date(a.lastFinishedAt).toLocaleString() : ""]].forEach(function (kv) {
      if (kv[1]) more.appendChild(el("div", null, kv[0] + ": " + kv[1]));
    });
    if (s.note) more.appendChild(el("div", "note", "Your note: " + s.note));
    more.appendChild(actions(s, r));
    r.appendChild(more);
    return r;
  }

  function actions(s, r) {
    var box = el("div", "actions");
    box.addEventListener("click", function (e) { e.stopPropagation(); });
    box.addEventListener("keydown", function (e) { e.stopPropagation(); });
    if (s.provider === "codex" && /^[0-9a-f][0-9a-f-]{19,}$/i.test(s.endpoint || "")) {
      var open = el("a", "action", "Open in Codex");
      open.href = "codex://threads/" + s.endpoint;
      open.title = "Steer it in Codex, which owns the session";
      box.appendChild(open);
    }
    if (s.managed) {
      var work = el("a", "action", "Managed run on the board");
      work.href = "/constellations";
      box.appendChild(work);
    }
    var noteBtn = el("button", "action", s.note ? "Edit note" : "Leave a note");
    noteBtn.type = "button";
    noteBtn.addEventListener("click", function () { editNote(s, r, box); });
    box.appendChild(noteBtn);
    return box;
  }

  function editNote(s, r, box) {
    r.dataset.editing = "1";
    var ed = el("div", "editor");
    var text = el("textarea");
    text.rows = 3;
    text.maxLength = 4000;
    text.value = s.note || "";
    text.setAttribute("aria-label", "Note for " + s.title);
    var save = el("button", "action primary", "Save note");
    var cancel = el("button", "action", "Cancel");
    save.type = cancel.type = "button";
    var status = el("span", "status");
    function done() { delete r.dataset.editing; delete r.dataset.sig; if (last) render(last); }
    cancel.addEventListener("click", done);
    save.addEventListener("click", function () {
      save.disabled = true;
      status.textContent = "Saving…";
      fetch("/api/live/note", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-KE-Board-Token": (last && last.controlToken) || "" },
        body: JSON.stringify({ agentId: s.id, text: text.value })
      }).then(function (res) { return res.json().then(function (j) { if (!res.ok) throw new Error(j.error || ("HTTP " + res.status)); return j; }); })
        .then(function () { status.textContent = "Saved"; delete r.dataset.editing; delete r.dataset.sig; load(); })
        .catch(function (e) { save.disabled = false; status.textContent = "Not saved: " + e.message; });
    });
    ed.appendChild(text);
    ed.appendChild(el("p", "hint", "The agent reads this at its next step. Nothing is interrupted."));
    var buttons = el("div", "actions");
    buttons.appendChild(save);
    buttons.appendChild(cancel);
    buttons.appendChild(status);
    ed.appendChild(buttons);
    ed.addEventListener("click", function (e) { e.stopPropagation(); });
    ed.addEventListener("keydown", function (e) { e.stopPropagation(); });
    box.replaceWith(ed);
    text.focus();
  }

  function reconcile(parent, nodes) {
    // Move nodes only when their position really changes, so focus, hover and open editors survive a refresh.
    nodes.forEach(function (node, i) { if (parent.children[i] !== node) parent.insertBefore(node, parent.children[i] || null); });
    while (parent.children.length > nodes.length) parent.removeChild(parent.lastChild);
  }

  function heading(project, group) {
    var key = project + "\u0000" + group, h = headings.get(key);
    if (!h) { h = el("div", "group", group); headings.set(key, h); }
    return h;
  }

  function matches(s) {
    if (onlyProject && s.project !== onlyProject) return false;
    if (!query) return true;
    var w = s.workingOn ? s.workingOn.text : "";
    return (s.title + " " + (s.team || "") + " " + s.project + " " + w).toLowerCase().indexOf(query) !== -1;
  }

  function panel(name) {
    var p = panels.get(name);
    if (!p) {
      p = el("section", "project");
      var head = el("div", "phead");
      head.tabIndex = 0;
      head.setAttribute("role", "button");
      head.appendChild(el("span", "pname", name || "No project"));
      head.appendChild(el("span", "pcounts"));
      var body = el("div", "pbody");
      function toggle() {
        p.classList.toggle("collapsed");
        collapsed[name] = p.classList.contains("collapsed");
        try { localStorage.setItem("live.collapsed", JSON.stringify(collapsed)); } catch (e) { /* private window */ }
      }
      head.addEventListener("click", toggle);
      head.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      p.appendChild(head);
      p.appendChild(body);
      if (collapsed[name]) p.classList.add("collapsed");
      panels.set(name, p);
    }
    return p;
  }

  function counts(list) {
    var c = { need: 0, work: 0, idle: 0 };
    list.forEach(function (s) { if (s.bucket === "needs-you") c.need++; else if (s.bucket === "working") c.work++; else c.idle++; });
    return c;
  }

  function fillCounts(node, c) {
    while (node.firstChild) node.removeChild(node.firstChild);
    var parts = [];
    if (c.need) parts.push(["need", c.need + " need you"]);
    if (c.work) parts.push(["work", c.work + " working"]);
    if (c.idle) parts.push([null, c.idle + " idle"]);
    parts.forEach(function (p, i) {
      if (i) node.appendChild(document.createTextNode(" · "));
      node.appendChild(el("span", p[0], p[1]));
    });
  }

  function render(data) {
    last = data;
    var byProject = new Map();
    data.sessions.forEach(function (s) {
      var key = s.project || "";
      if (!byProject.has(key)) byProject.set(key, []);
      byProject.get(key).push(s);
    });
    var names = Array.from(byProject.keys());
    var sel = $("project"), current = sel.value;
    var labels = names.filter(Boolean);
    if (sel.options.length - 1 !== labels.length || labels.some(function (n, i) { return sel.options[i + 1].value !== n; })) {
      while (sel.options.length > 1) sel.remove(1);
      labels.forEach(function (n) { var o = el("option", null, n); o.value = n; sel.appendChild(o); });
      sel.value = labels.indexOf(current) >= 0 ? current : "";
    }

    var main = $("projects"), backlog = [], shownProjects = 0, order = [];
    names.forEach(function (name) {
      var list = byProject.get(name);
      var active = list.some(function (s) { return ACTIVE[s.bucket]; });
      if (!active && filter !== "all") {
        list.forEach(function (s) { if (s.bucket === "old-request" && matches(s)) backlog.push(s); });
        return;
      }
      var visible = list.filter(matches);
      if (!visible.length) return;
      var p = panel(name);
      fillCounts(p.querySelector(".pcounts"), counts(list));
      var body = p.querySelector(".pbody");
      var nodes = [], group = null;
      visible.forEach(function (s) {
        if (s.group !== group) { group = s.group; nodes.push(heading(name, group)); }
        nodes.push(row(s));
      });
      reconcile(body, nodes);
      order.push(p);
      shownProjects++;
    });
    reconcile(main, order);
    $("empty").hidden = shownProjects > 0;

    var bl = $("backlog"), blRows = $("backlog-rows");
    bl.hidden = backlog.length === 0;
    bl.querySelector(".count").textContent = backlog.length ? "· " + backlog.length : "";
    reconcile(blRows, backlog.map(row));

    var need = data.sessions.filter(function (s) { return s.bucket === "needs-you"; });
    var att = $("attention"), list = $("att-list");
    att.hidden = need.length === 0;
    while (list.firstChild) list.removeChild(list.firstChild);
    need.forEach(function (s) {
      var b = el("button", "att", s.title + (s.project ? " · " + s.project : "") + ": " + stateText(s));
      b.type = "button";
      b.addEventListener("click", function () {
        var r = rows.get(s.id);
        if (!r) return;
        var p = r.closest(".project");
        if (p && p.classList.contains("collapsed")) p.classList.remove("collapsed");
        r.classList.add("open");
        r.scrollIntoView({ block: "center", behavior: "smooth" });
        r.focus({ preventScroll: true });
      });
      list.appendChild(b);
    });

    var n = data.counts || {};
    var summary = $("summary");
    while (summary.firstChild) summary.removeChild(summary.firstChild);
    [["need", n["needs-you"], "need you"], ["work", n.working, "working now"], [null, n.recent, "finished in the last 12 h"]].forEach(function (p, i) {
      if (i) summary.appendChild(document.createTextNode(" · "));
      summary.appendChild(el("b", p[0], String(p[1] || 0)));
      summary.appendChild(document.createTextNode(" " + p[2]));
    });
    if (n["old-request"]) summary.appendChild(document.createTextNode(" · " + n["old-request"] + (n["old-request"] === 1 ? " older request" : " older requests")));
    $("updated").textContent = "Updated " + new Date(epoch(data.sampledAt) || Date.now()).toLocaleTimeString();
  }

  function schedule(ms) { clearTimeout(timer); if (!document.hidden) timer = setTimeout(load, ms); }

  function load() {
    fetch("/api/live" + (filter === "all" ? "?all=1" : ""), { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (res) { if (!res.ok) throw new Error("HTTP " + res.status); return res.json(); })
      .then(function (data) { failures = 0; $("error").hidden = true; render(data); schedule(1000 * (data.pollSeconds || 3)); })
      .catch(function () {
        failures += 1;
        $("error").textContent = "The board is not answering. Retrying…";
        $("error").hidden = false;
        schedule(Math.min(30000, 3000 * failures));
      });
  }

  document.querySelectorAll(".chip").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".chip").forEach(function (x) { x.classList.toggle("on", x === b); });
      filter = b.dataset.filter;
      load();
    });
  });
  $("project").addEventListener("change", function (e) { onlyProject = e.target.value; if (last) render(last); });
  $("search").addEventListener("input", function (e) { query = e.target.value.trim().toLowerCase(); if (last) render(last); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) load(); else clearTimeout(timer); });
  load();
})();
