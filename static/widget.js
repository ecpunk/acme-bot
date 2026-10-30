(function () {
  "use strict";

  var bubble = document.getElementById("acme-bubble");
  var panel = document.getElementById("acme-panel");
  var closeBtn = document.getElementById("acme-close");
  var form = document.getElementById("acme-form");
  var input = document.getElementById("acme-input");
  var log = document.getElementById("acme-log");

  var sessionId = null;
  var opening = false;

  setTimeout(function () {
    bubble.hidden = false;
    // Next frame so the transition actually runs.
    requestAnimationFrame(function () {
      bubble.classList.add("acme-visible");
    });
  }, 3000);

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // The model is told (prompts/system_v1.md) to send plain text only — no
  // Markdown, no asterisks. This is the fallback for when it doesn't
  // listen: render **bold** as real bold, then strip any stray asterisks
  // left over so the guest never sees raw "**".
  function renderText(text) {
    var escaped = escapeHtml(text == null ? "" : text);
    var withBold = escaped.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return withBold.replace(/\*/g, "");
  }

  function appendMessage(role, text, toolCalls) {
    var wrap = document.createElement("div");
    wrap.className = "acme-msg " + role;
    var label = document.createElement("div");
    label.className = "acme-msg-label";
    label.textContent = role === "assistant" ? "Sam" : "You";
    wrap.appendChild(label);
    var bubbleEl = document.createElement("div");
    bubbleEl.className = "bubble";
    bubbleEl.innerHTML = renderText(text);
    wrap.appendChild(bubbleEl);
    if (toolCalls && toolCalls.length) {
      for (var i = 0; i < toolCalls.length; i++) {
        var line = document.createElement("div");
        line.className = "acme-tool-line";
        line.textContent = "tool: " + toolCalls[i].tool;
        wrap.appendChild(line);
      }
    }
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  // Static welcome, shown immediately — no model call, not sent to the
  // API. The model's own first reply follows the guest's first message,
  // same as before. Guarded on `#acme-log` being empty so it never
  // double-appends, and called both defensively (in case the panel is
  // ever visible without openPanel having run — see the widget.css
  // [hidden] fix alongside this) and from openPanel itself.
  function showWelcomeIfNeeded() {
    if (!log.childElementCount) {
      appendMessage("assistant", "Hi, I'm Sam from Acme Guest Services. Are you planning a visit, or checking on a booking?");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!panel.hidden) {
      showWelcomeIfNeeded();
    }
  });

  async function ensureSession() {
    if (sessionId) return sessionId;
    var res = await fetch("/api/session", { method: "POST", credentials: "same-origin" });
    if (!res.ok) throw new Error("could not open session");
    var data = await res.json();
    sessionId = data.session_id;
    return sessionId;
  }

  async function openPanel() {
    if (opening) return;
    opening = true;
    bubble.hidden = true;
    panel.hidden = false;
    showWelcomeIfNeeded();
    try {
      await ensureSession();
    } catch (err) {
      appendMessage("assistant", "Sorry, I couldn't start a chat session right now.");
    } finally {
      opening = false;
      input.focus();
    }
  }

  function closePanel() {
    panel.hidden = true;
  }

  bubble.addEventListener("click", openPanel);
  closeBtn.addEventListener("click", closePanel);

  form.addEventListener("submit", async function (evt) {
    evt.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    appendMessage("user", text);
    try {
      var sid = await ensureSession();
      var res = await fetch("/api/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sid, message: text }),
      });
      if (!res.ok) {
        appendMessage("assistant", "Sorry, something went wrong (" + res.status + ").");
        return;
      }
      var data = await res.json();
      appendMessage("assistant", data.reply, data.tool_calls);
    } catch (err) {
      appendMessage("assistant", "Sorry, something went wrong.");
    }
  });
})();
