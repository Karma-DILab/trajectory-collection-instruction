// Injected into every page (and every navigation) via context.add_init_script.
//
// Two jobs:
//  1. Capture raw user input (mousedown / keydown / wheel) and forward it to
//     Python via window.__webtrack_event.
//  2. Host the THOUGHT PANEL *inside the page* (right-docked card) so there is
//     exactly one OS window and no cross-window keyboard-focus fight. While a
//     thought is unanswered the page is dimmed + input-blocked; the card takes
//     the thought and submits it via window.__webtrack_thought(step, text).
//
// Anything under an element marked [data-webtrack] is OUR UI: its events are
// never recorded and never blocked, so typing a thought is not mistaken for a
// page `type` and the submit button always works.

(function () {
  if (window.__webtrack_injected) return;
  window.__webtrack_injected = true;

  function send(payload) {
    try { window.__webtrack_event(payload); } catch (e) { /* host not ready */ }
  }

  function isOurs(node) {
    try { return !!(node && node.closest && node.closest("[data-webtrack]")); }
    catch (_) { return false; }
  }

  // ============================ in-page panel ============================

  var UI_ID = "__webtrack_panel";
  var DIM_ID = "__webtrack_dim";
  var LOCK_EVS = ["mousedown", "keydown", "wheel", "contextmenu"];
  var els = null;          // cached panel widgets once built
  var currentStep = null;  // step of the card currently shown
  var refocusEl = null;    // page element to restore focus to on unlock

  // Block page input while a thought is pending. Events on our own UI pass
  // through untouched so the textarea / buttons stay usable. We only block the
  // action-INITIATING events (never click/mouseup/keyup), so the very click or
  // Enter that triggered the lock still completes its navigation.
  function blockEvt(e) {
    if (isOurs(e.target)) return;
    try { e.preventDefault(); } catch (_) {}
    e.stopImmediatePropagation();
  }
  function addBlockers() {
    if (window.__wt_blocking) return;
    window.__wt_blocking = true;
    LOCK_EVS.forEach(function (t) {
      window.addEventListener(t, blockEvt, { capture: true, passive: false });
    });
  }
  function removeBlockers() {
    if (!window.__wt_blocking) return;
    window.__wt_blocking = false;
    LOCK_EVS.forEach(function (t) {
      window.removeEventListener(t, blockEvt, true);
    });
  }

  function css(el, s) { el.setAttribute("style", s); }

  function buildPanel() {
    if (els) return;
    var host = document.body || document.documentElement;
    if (!host) return;

    var dim = document.createElement("div");
    dim.id = DIM_ID;
    dim.setAttribute("data-webtrack", "1");
    css(dim, "position:fixed;inset:0;z-index:2147483646;display:none;" +
            "background:rgba(15,17,21,0.45);pointer-events:none;");

    var panel = document.createElement("div");
    panel.id = UI_ID;
    panel.setAttribute("data-webtrack", "1");
    css(panel,
      "position:fixed;top:0;right:0;width:430px;max-width:46vw;height:100vh;" +
      "z-index:2147483647;display:none;flex-direction:column;gap:8px;" +
      "box-sizing:border-box;padding:18px 18px 14px;overflow:auto;" +
      "background:#ffffff;box-shadow:-6px 0 28px rgba(0,0,0,.35);" +
      "font-family:'Segoe UI',system-ui,-apple-system,sans-serif;color:#202124;");

    var task = document.createElement("div");
    css(task, "color:#8a8f98;font-size:12px;line-height:1.4;white-space:pre-wrap;");

    var obsLbl = document.createElement("div");
    obsLbl.textContent = "관찰 (스크린샷)";
    css(obsLbl, "color:#0a7d4b;font-size:12px;font-weight:700;margin-top:4px;");
    var img = document.createElement("img");
    css(img, "width:100%;border:1px solid #e3e6ea;border-radius:6px;display:none;");
    var imgPh = document.createElement("div");
    imgPh.textContent = "(스크린샷 대기...)";
    css(imgPh, "color:#8a8f98;font-size:12px;padding:24px;text-align:center;" +
              "background:#f3f4f6;border-radius:6px;");

    var actLbl = document.createElement("div");
    actLbl.textContent = "행동";
    css(actLbl, "color:#1a73e8;font-size:12px;font-weight:700;margin-top:6px;");
    var action = document.createElement("div");
    css(action, "font-family:Consolas,monospace;font-size:14px;background:#f3f4f6;" +
               "border-radius:6px;padding:8px 10px;white-space:pre-wrap;");

    var thoughtLbl = document.createElement("div");
    thoughtLbl.textContent = "생각  (직접 작성, 필수)";
    css(thoughtLbl, "color:#7b2ff7;font-size:12px;font-weight:700;margin-top:6px;");
    var ta = document.createElement("textarea");
    ta.setAttribute("data-webtrack", "1");
    ta.setAttribute("rows", "5");
    ta.setAttribute("placeholder", "무엇이 보여서 → 무엇을 하려고 → 어떤 행동을 했다");
    css(ta, "width:100%;box-sizing:border-box;resize:vertical;font-size:14px;" +
           "font-family:inherit;border:1px solid #c9ccd1;border-radius:6px;padding:8px;");

    var save = document.createElement("button");
    save.setAttribute("data-webtrack", "1");
    save.textContent = "확인  (Enter)";
    css(save, "margin-top:4px;padding:10px;border:0;border-radius:6px;cursor:pointer;" +
             "background:#1a73e8;color:#fff;font-size:14px;font-weight:700;");

    var status = document.createElement("div");
    css(status, "color:#8a8f98;font-size:12px;text-align:right;");

    panel.appendChild(task);
    panel.appendChild(obsLbl);
    panel.appendChild(img); panel.appendChild(imgPh);
    panel.appendChild(actLbl); panel.appendChild(action);
    panel.appendChild(thoughtLbl); panel.appendChild(ta);
    panel.appendChild(save); panel.appendChild(status);

    host.appendChild(dim);
    host.appendChild(panel);

    els = { dim: dim, panel: panel, task: task, img: img, imgPh: imgPh,
            action: action, ta: ta, save: save, status: status };

    save.addEventListener("click", submit, false);
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
    }, false);
  }

  // Lock / unlock the page. Locking shows ONLY the dim + blocks input — the
  // card itself is revealed by showCard() once it has content and can grab the
  // textarea focus. This avoids a window where an empty/stale panel is visible
  // but unfocused, where a stray Enter would land on the page instead.
  function setLock(on) {
    buildPanel();
    if (!els) return;
    if (on) {
      if (!window.__wt_blocking) {
        // remember the page's focused field (once, on the first lock)
        try { refocusEl = document.activeElement; } catch (_) { refocusEl = null; }
        if (isOurs(refocusEl)) refocusEl = null;
      }
      els.dim.style.display = "block";
      addBlockers();
    } else {
      els.dim.style.display = "none";
      els.panel.style.display = "none";
      removeBlockers();
      currentStep = null;
      // hand keyboard focus back to wherever it was on the page
      try {
        if (refocusEl && refocusEl.focus && document.contains(refocusEl)) refocusEl.focus();
      } catch (_) {}
      refocusEl = null;
    }
  }
  window.__webtrack_setlock = setLock;

  // Populate the card with the action Python just recorded and focus the
  // textarea so the user can type the thought immediately (same window -> the
  // focus always sticks).
  function showCard(payload) {
    buildPanel();
    if (!els || !payload) return;
    currentStep = payload.step;
    els.task.textContent = "Task: " + (payload.task || "");
    els.action.textContent = payload.summary || "";
    if (payload.img) {
      els.img.src = payload.img; els.img.style.display = "block";
      els.imgPh.style.display = "none";
    } else {
      els.img.style.display = "none"; els.imgPh.style.display = "block";
    }
    els.ta.value = "";
    els.save.disabled = false;
    els.status.textContent = "남은 입력: " + (payload.pending || 1);
    setLock(true);                       // dim + block
    els.panel.style.display = "flex";    // reveal the card (now it has content)
    try { els.ta.focus(); } catch (_) {} // focus is reliable: same window, DOM
  }
  window.__webtrack_showcard = showCard;

  function submit() {
    if (!els || currentStep == null) return;
    var text = (els.ta.value || "").trim();
    if (!text) { try { els.ta.focus(); } catch (_) {} return; }
    var step = currentStep;
    els.save.disabled = true;
    els.status.textContent = "저장 중...";
    try { window.__webtrack_thought(step, text); } catch (_) {}
    // Python responds by either showing the next queued card or unlocking.
  }

  // ============================ input capture ============================

  // Left clicks (button 0)
  document.addEventListener("mousedown", function (e) {
    if (e.button !== 0 || isOurs(e.target)) return;
    send({ type: "click", button: e.button,
           x: Math.round(e.clientX), y: Math.round(e.clientY) });
    setLock(true);   // every left click is a recorded action -> lock the next
  }, true);

  // Keyboard
  document.addEventListener("keydown", function (e) {
    if (e.repeat || isOurs(e.target)) return;
    send({ type: "keydown", key: e.key, code: e.code,
           ctrlKey: e.ctrlKey, altKey: e.altKey,
           shiftKey: e.shiftKey, metaKey: e.metaKey });
    var k = e.key || "";
    var printable = (k.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey);
    var modifier = (k === "Shift" || k === "Control" || k === "Alt" || k === "Meta");
    if (!printable && !modifier && k !== "Backspace") setLock(true);
  }, true);

  // Wheel scroll (Python side debounces / accumulates)
  document.addEventListener("wheel", function (e) {
    if (isOurs(e.target)) return;
    send({ type: "wheel", deltaY: e.deltaY });
  }, true);

  // ===================== single-tab enforcement =====================

  try {
    window.open = function (url) {
      if (url) { try { window.location.href = url; } catch (e) {} }
      return null;
    };
  } catch (e) {}

  function stripAnchorTarget(a) {
    try {
      if (!a) return;
      const t = a.getAttribute && a.getAttribute("target");
      if (t && t.toLowerCase() === "_blank") a.removeAttribute("target");
      a.target = "_self";
    } catch (e) {}
  }
  function stripAll(root) {
    try {
      if (!root || !root.querySelectorAll) return;
      root.querySelectorAll("a[target]").forEach(stripAnchorTarget);
    } catch (e) {}
  }
  stripAll(document);
  try {
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const n of m.addedNodes) {
          if (n && n.nodeType === 1) {
            if (n.tagName === "A") stripAnchorTarget(n);
            stripAll(n);
          }
        }
      }
    }).observe(document.documentElement || document, { childList: true, subtree: true });
  } catch (e) {}

  document.addEventListener("click", function (e) {
    if (!(e.ctrlKey || e.metaKey || e.button === 1)) return;
    if (isOurs(e.target)) return;
    const a = e.target && e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    e.preventDefault(); e.stopPropagation();
    try { window.location.href = a.href; } catch (err) {}
  }, true);

  document.addEventListener("auxclick", function (e) {
    if (e.button === 1 && !isOurs(e.target)) {
      const a = e.target && e.target.closest && e.target.closest("a[href]");
      if (a) {
        e.preventDefault(); e.stopPropagation();
        try { window.location.href = a.href; } catch (err) {}
      }
    }
  }, true);

  // ===================== re-show across navigation =====================
  // A navigating action (e.g. a search) lands us on a FRESH page whose card
  // hasn't been re-shown yet. We block input SYNCHRONOUSLY from the start, then
  // ask Python what to do:
  //   {step,...}    -> re-show this unanswered card (stay locked)
  //   {busy:true}   -> Python is still recording the action that brought us
  //                    here; keep polling, the card is about to appear
  //   null          -> idle; release the page
  // Without this the page would unlock before the search's card appears and the
  // Enter meant to save the thought would leak onto the page (bogus key Enter).
  addBlockers();
  function wtCheck(n) {
    var fn = window.__webtrack_pending_card;
    if (!fn) {                               // binding not installed yet — wait
      if (n < 60) setTimeout(function () { wtCheck(n + 1); }, 50);
      else removeBlockers();
      return;
    }
    fn().then(function (r) {
      if (r && r.step != null) { showCard(r); return; }     // card ready -> show
      var wait = (r && r.busy) ? (n < 60)   // busy: wait up to ~3s for the card
                                : (n < 5);   // idle: tiny grace for in-flight event
      if (wait) { setTimeout(function () { wtCheck(n + 1); }, 50); return; }
      removeBlockers();                       // confidently idle -> release
    }).catch(function () { removeBlockers(); });
  }
  wtCheck(0);
})();
