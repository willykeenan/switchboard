/* Agents, live: every agent at once. Read-only; polls /api/live and never touches a session. */
(function () {
  "use strict";
  var filter = "recent", project = "", query = "", timer = null, cards = new Map(), last = null, failures = 0;
  var $ = function (id) { return document.getElementById(id); };
  var PHASE_WORDS = { tools: "Using a tool…", waiting: "Waiting on a tool or timer…" };

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

  function card(s) {
    var c = cards.get(s.id);
    if (!c) {
      c = el("article", "card");
      c.tabIndex = 0;
      c.setAttribute("role", "button");
      c.addEventListener("click", function () { c.classList.toggle("open"); c.setAttribute("aria-expanded", c.classList.contains("open")); });
      c.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); c.click(); } });
      cards.set(s.id, c);
    }
    var a = s.activity || {};
    c.dataset.bucket = s.bucket;
    c.dataset.phase = a.phase || "unknown";
    c.setAttribute("aria-expanded", c.classList.contains("open"));
    if (c.dataset.editing === "1") return c;  // never wipe a note while it is being written
    var said0 = a.publicAction || (s.bucket === "working" || s.bucket === "needs-you" ? a.lastAction : null);
    var turn0 = a.active && typeof a.durationSeconds === "number" ? span(a.durationSeconds) : "";
    var sig = JSON.stringify([s.title, s.provider, s.bucket, a.phase, a.label, said0, a.planProgress, s.project, s.role,
      turn0, a.lastFinishedDurationSeconds, a.model, a.reasoning, a.turnStatus, a.lastFinishedAt, s.note, s.managed, s.endpoint]);
    var seenAt = epoch(a.observedAt) || epoch(s.lastSeenAt);
    if (c.dataset.sig === sig) {  // unchanged: only refresh the relative time, so buttons never move under the pointer
      var u = c.querySelector(".seen");
      if (u && seenAt) u.textContent = "updated " + ago(seenAt);
      return c;
    }
    c.dataset.sig = sig;
    while (c.firstChild) c.removeChild(c.firstChild);

    var row = el("div", "row");
    row.appendChild(el("div", "name", s.title));
    row.appendChild(el("span", "tag " + (s.provider || ""), s.provider === "claude" ? "Claude" : s.provider === "codex" ? "Codex" : (s.provider || "")));
    c.appendChild(row);

    var state = el("div", "state");
    state.appendChild(el("span", "pip"));
    state.appendChild(el("span", null, PHASE_WORDS[a.phase] || a.label || "Activity unknown"));
    c.appendChild(state);

    var said = a.publicAction || (s.bucket === "working" || s.bucket === "needs-you" ? a.lastAction : null);
    if (said) c.appendChild(el("p", "said", said));

    var p = a.planProgress;
    if (p && p.total) {
      var bar = el("div", "progress");
      bar.title = p.completed + " of " + p.total + " plan steps done";
      var fill = el("i");
      fill.style.width = Math.round(100 * p.completed / p.total) + "%";
      bar.appendChild(fill);
      c.appendChild(bar);
    }

    var meta = el("div", "meta");
    if (s.project) meta.appendChild(el("span", null, s.project));
    if (s.role) meta.appendChild(el("span", null, s.role));
    if (a.active && typeof a.durationSeconds === "number") meta.appendChild(el("span", null, "turn " + span(a.durationSeconds)));
    else if (typeof a.lastFinishedDurationSeconds === "number" && s.bucket === "recent") meta.appendChild(el("span", null, "last turn " + span(a.lastFinishedDurationSeconds)));
    if (seenAt) meta.appendChild(el("span", "seen", "updated " + ago(seenAt)));
    if (p && p.total) meta.appendChild(el("span", null, p.completed + "/" + p.total + " steps"));
    c.appendChild(meta);

    var more = el("div", "more");
    [["Session", s.id], ["Model", [a.model, a.reasoning].filter(Boolean).join(" · ")], ["Turn", a.turnStatus],
     ["Last finished", a.lastFinishedAt ? new Date(a.lastFinishedAt).toLocaleString() : ""],
     ["Managed run", s.managed ? "yes" : ""]].forEach(function (kv) {
      if (kv[1]) more.appendChild(el("div", null, kv[0] + ": " + kv[1]));
    });
    if (s.note) more.appendChild(el("div", "note", "Your note: " + s.note));
    more.appendChild(actions(s, c));
    c.appendChild(more);
    return c;
  }

  function actions(s, c) {
    var row = el("div", "actions");
    row.addEventListener("click", function (e) { e.stopPropagation(); });
    row.addEventListener("keydown", function (e) { e.stopPropagation(); });
    if (s.provider === "codex" && /^[0-9a-f][0-9a-f-]{19,}$/i.test(s.endpoint || "")) {
      var open = el("a", "action", "Open in Codex");
      open.href = "codex://threads/" + s.endpoint;
      open.title = "Steer it in Codex, which owns the session";
      row.appendChild(open);
    }
    if (s.managed) {
      var work = el("a", "action", "Managed run on the board");
      work.href = "/constellations";
      row.appendChild(work);
    }
    var noteBtn = el("button", "action", s.note ? "Edit note" : "Leave a note");
    noteBtn.type = "button";
    noteBtn.addEventListener("click", function () { editNote(s, c, row); });
    row.appendChild(noteBtn);
    return row;
  }

  function editNote(s, c, row) {
    c.dataset.editing = "1";
    var box = el("div", "editor");
    var text = el("textarea");
    text.rows = 3;
    text.maxLength = 4000;
    text.value = s.note || "";
    text.setAttribute("aria-label", "Note for " + s.title);
    var hint = el("p", "hint", "The agent reads this at its next step. Nothing is interrupted.");
    var save = el("button", "action primary", "Save note");
    var cancel = el("button", "action", "Cancel");
    save.type = cancel.type = "button";
    var status = el("span", "status");
    function done() { delete c.dataset.editing; delete c.dataset.sig; if (last) render(last); }
    cancel.addEventListener("click", done);
    save.addEventListener("click", function () {
      save.disabled = true;
      status.textContent = "Saving…";
      fetch("/api/live/note", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-KE-Board-Token": (last && last.controlToken) || "" },
        body: JSON.stringify({ agentId: s.id, text: text.value })
      }).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || ("HTTP " + r.status)); return j; }); })
        .then(function () { status.textContent = "Saved"; delete c.dataset.editing; delete c.dataset.sig; load(); })
        .catch(function (e) { save.disabled = false; status.textContent = "Not saved: " + e.message; });
    });
    box.appendChild(text);
    box.appendChild(hint);
    var buttons = el("div", "actions");
    buttons.appendChild(save);
    buttons.appendChild(cancel);
    buttons.appendChild(status);
    box.appendChild(buttons);
    box.addEventListener("click", function (e) { e.stopPropagation(); });
    box.addEventListener("keydown", function (e) { e.stopPropagation(); });
    row.replaceWith(box);
    text.focus();
  }

  function visible(s) {
    if (project && s.project !== project) return false;
    if (filter === "now" && s.bucket !== "working" && s.bucket !== "needs-you") return false;
    if (query) {
      var hay = (s.title + " " + s.project + " " + (s.role || "")).toLowerCase();
      if (hay.indexOf(query) === -1) return false;
    }
    return true;
  }

  function render(data) {
    last = data;
    var projects = {}, shown = 0;
    data.sessions.forEach(function (s) { if (s.project) projects[s.project] = 1; });
    var sel = $("project"), current = sel.value;
    var names = Object.keys(projects).sort();
    if (sel.options.length - 1 !== names.length || names.some(function (n, i) { return sel.options[i + 1].value !== n; })) {
      while (sel.options.length > 1) sel.remove(1);
      names.forEach(function (n) { var o = el("option", null, n); o.value = n; sel.appendChild(o); });
      sel.value = names.indexOf(current) >= 0 ? current : "";
    }
    var groups = { "needs-you": [], working: [], recent: [], "old-request": [], quiet: [] };
    data.sessions.forEach(function (s) { if (visible(s)) groups[s.bucket].push(s); });
    Object.keys(groups).forEach(function (b) {
      var section = document.querySelector('section[data-bucket="' + b + '"]');
      var grid = section.querySelector(".grid");
      var list = groups[b];
      section.hidden = list.length === 0;
      section.querySelector(".count").textContent = list.length ? "· " + list.length : "";
      var keep = new Set();
      list.forEach(function (s) { var c = card(s); keep.add(c); grid.appendChild(c); });
      Array.prototype.slice.call(grid.children).forEach(function (c) { if (!keep.has(c)) grid.removeChild(c); });
      shown += list.length;
    });
    $("empty").hidden = shown > 0;

    var n = data.counts || {};
    var summary = $("summary");
    while (summary.firstChild) summary.removeChild(summary.firstChild);
    var parts = [["need", n["needs-you"], "need you"], ["work", n.working, "working now"], [null, n.recent, "finished in the last 12 h"]];
    if (n["old-request"]) parts.push([null, n["old-request"], n["old-request"] === 1 ? "older request" : "older requests"]);
    parts.forEach(function (p, i) {
      if (i) summary.appendChild(document.createTextNode(" · "));
      var b = el("b", p[0], String(p[1] || 0));
      summary.appendChild(b);
      summary.appendChild(document.createTextNode(" " + p[2]));
    });
    if (!n.working && !n["needs-you"]) summary.appendChild(document.createTextNode(" · nobody is working right now"));
    $("updated").textContent = "Updated " + new Date(epoch(data.sampledAt) || Date.now()).toLocaleTimeString();
  }

  function schedule(ms) { clearTimeout(timer); if (!document.hidden) timer = setTimeout(load, ms); }

  function load() {
    fetch("/api/live" + (filter === "all" ? "?all=1" : ""), { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        failures = 0;
        $("error").hidden = true;
        render(data);
        schedule(1000 * (data.pollSeconds || 3));
      })
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
      var before = filter;
      filter = b.dataset.filter;
      if ((before === "all") !== (filter === "all")) load(); else if (last) render(last);
    });
  });
  $("project").addEventListener("change", function (e) { project = e.target.value; if (last) render(last); });
  $("search").addEventListener("input", function (e) { query = e.target.value.trim().toLowerCase(); if (last) render(last); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) load(); else clearTimeout(timer); });
  load();
})();
