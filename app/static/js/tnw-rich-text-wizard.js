/**
 * Minimal rich-text helpers for the event wizard (mirrors platform dashboard behaviour).
 */
(function () {
  "use strict";

  function sanitizeRichHtml(html) {
    var allowedTags = {
      b: true,
      strong: true,
      i: true,
      em: true,
      u: true,
      ul: true,
      ol: true,
      li: true,
      p: true,
      div: true,
      br: true,
    };
    var template = document.createElement("template");
    template.innerHTML = html || "";

    function cleanNode(node) {
      if (node.nodeType === Node.TEXT_NODE) {
        return document.createTextNode(node.textContent || "");
      }
      if (node.nodeType !== Node.ELEMENT_NODE) {
        return document.createDocumentFragment();
      }

      var tag = node.tagName.toLowerCase();
      var cleaned = allowedTags[tag]
        ? document.createElement(tag)
        : document.createDocumentFragment();

      if (tag === "br" && allowedTags[tag]) {
        return cleaned;
      }

      Array.from(node.childNodes).forEach(function (child) {
        cleaned.appendChild(cleanNode(child));
      });
      return cleaned;
    }

    var output = document.createElement("div");
    Array.from(template.content.childNodes).forEach(function (child) {
      output.appendChild(cleanNode(child));
    });
    return output.innerHTML.trim();
  }

  function plainTextToRichHtml(text) {
    var raw = (text || "").replace(/\r\n/g, "\n").trim();
    if (!raw) return "";
    return raw
      .split(/\n{2,}/)
      .map(function (paragraph) {
        var escapedLines = paragraph.split("\n").map(function (line) {
          var span = document.createElement("span");
          span.textContent = line;
          return span.innerHTML;
        });
        return "<p>" + escapedLines.join("<br>") + "</p>";
      })
      .join("");
  }

  function richValueToEditorHtml(value) {
    var raw = (value || "").trim();
    if (!raw) return "";
    if (/<\/?(p|div|br|strong|b|em|i|u|ul|ol|li)\b/i.test(raw)) {
      return sanitizeRichHtml(raw);
    }
    return plainTextToRichHtml(raw);
  }

  function syncRichEditorToInput(editor, shouldDispatch) {
    var inputId = editor ? editor.getAttribute("data-rich-input") : "";
    var input = inputId ? document.getElementById(inputId) : null;
    if (!editor || !input) return;
    var text = (editor.innerText || "").trim();
    input.value = text ? sanitizeRichHtml(editor.innerHTML) : "";
    if (shouldDispatch !== false) {
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  function setRichInputValue(input, html) {
    if (!input) return;
    input.value = html || "";
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function getRichEditorForInput(input) {
    if (!input || !input.id) return null;
    return document.querySelector(
      '.js-rich-text-editor[data-rich-input="' + CSS.escape(input.id) + '"]'
    );
  }

  function wireGroupDescriptionCopy(scope) {
    var root = scope && scope.querySelector ? scope : document;
    root.querySelectorAll(".js-copy-group-description-btn").forEach(function (btn) {
      if (btn.getAttribute("data-tnw-copy-wired") === "1") return;
      btn.setAttribute("data-tnw-copy-wired", "1");
      btn.addEventListener("click", function () {
        var sourceId = btn.getAttribute("data-copy-source");
        var targetId = btn.getAttribute("data-copy-target");
        var form = btn.closest("form");
        function fieldById(id) {
          if (!id) return null;
          if (form) {
            var scoped = form.querySelector("#" + CSS.escape(id));
            if (scoped) return scoped;
          }
          return document.getElementById(id);
        }
        var source = fieldById(sourceId);
        var target = fieldById(targetId);
        if (!source || !target) return;

        var groupDescription = (source.value || "").trim();
        if (!groupDescription) {
          if (window.showTnwMessage) {
            window.showTnwMessage("This group does not have a description to copy yet.", {
              title: "No group description",
            });
          }
          return;
        }

        if (target.classList.contains("js-rich-text-input")) {
          var html = richValueToEditorHtml(groupDescription);
          setRichInputValue(target, html);
          var editor = getRichEditorForInput(target);
          if (editor) {
            editor.innerHTML = html;
            syncRichEditorToInput(editor);
            editor.dispatchEvent(new Event("input", { bubbles: true }));
            editor.focus();
          }
        } else {
          target.value = groupDescription;
          target.focus();
          target.dispatchEvent(new Event("input", { bubbles: true }));
        }
      });
    });
  }

  function insertPlainTextAtSelection(text) {
    if (text == null) text = "";
    if (document.queryCommandSupported && document.queryCommandSupported("insertText")) {
      document.execCommand("insertText", false, text);
      return;
    }
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    range.deleteContents();
    var node = document.createTextNode(text);
    range.insertNode(node);
    range.setStartAfter(node);
    range.setEndAfter(node);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function wireRichTextPastePlain(editor, afterPaste) {
    if (!editor || editor.getAttribute("data-rich-paste-plain") === "1") return;
    editor.setAttribute("data-rich-paste-plain", "1");
    editor.addEventListener("paste", function (ev) {
      ev.preventDefault();
      var clip = ev.clipboardData || window.clipboardData;
      if (!clip) return;
      var text = clip.getData("text/plain");
      if (text == null) text = "";
      editor.focus();
      insertPlainTextAtSelection(text);
      if (typeof afterPaste === "function") afterPaste(editor);
    });
  }

  function wireRichTextEditors(scope) {
    var root = scope && scope.querySelector ? scope : document;
    root.querySelectorAll(".js-rich-text-editor").forEach(function (editor) {
      var input = document.getElementById(editor.getAttribute("data-rich-input") || "");
      if (!input) return;

      editor.innerHTML = richValueToEditorHtml(input.value);
      wireRichTextPastePlain(editor, syncRichEditorToInput);
      editor.addEventListener("input", function () {
        syncRichEditorToInput(editor);
      });
      input.addEventListener("input", function () {
        if (document.activeElement !== editor) {
          editor.innerHTML = richValueToEditorHtml(input.value);
        }
      });

      var form = input.closest("form");
      if (form && !form.dataset.richTextSubmitWired) {
        form.dataset.richTextSubmitWired = "1";
        form.addEventListener("submit", function (ev) {
          form.querySelectorAll(".js-rich-text-editor").forEach(function (ed) {
            syncRichEditorToInput(ed);
          });
          var requiredEditor = form.querySelector('.js-rich-text-editor[data-rich-required="1"]');
          if (requiredEditor && !(requiredEditor.innerText || "").trim()) {
            ev.preventDefault();
            requiredEditor.focus();
            if (window.showTnwMessage) {
              window.showTnwMessage("Description is required.", { title: "Description needed" });
            }
          }
        });
      }
    });

    root.querySelectorAll(".js-rich-format-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var editor = document.getElementById(btn.getAttribute("data-rich-target") || "");
        var action = btn.getAttribute("data-format-action");
        if (!editor) return;

        editor.focus();
        if (action === "bold") {
          document.execCommand("bold", false, null);
        } else if (action === "italic") {
          document.execCommand("italic", false, null);
        } else if (action === "underline") {
          document.execCommand("underline", false, null);
        } else if (action === "bullet") {
          document.execCommand("insertUnorderedList", false, null);
        } else if (action === "numbered") {
          document.execCommand("insertOrderedList", false, null);
        }
        syncRichEditorToInput(editor);
      });
    });
  }

  window.tnwRichValueToEditorHtml = richValueToEditorHtml;
  window.tnwSanitizeRichHtml = sanitizeRichHtml;
  window.tnwSyncRichEditorToInput = syncRichEditorToInput;
  window.tnwSetRichInputValue = setRichInputValue;
  function countWords(text) {
    return String(text || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean).length;
  }

  function wireMeetingDescriptionAiPolish(buttonId, textareaId, polishUrl, scope) {
    var root = scope && scope.querySelector ? scope : document;
    var btn =
      root.querySelector("#" + CSS.escape(buttonId)) || document.getElementById(buttonId);
    var ta =
      root.querySelector("#" + CSS.escape(textareaId)) || document.getElementById(textareaId);
    if (!btn || !ta || typeof polishUrl !== "string") return;
    if (btn.getAttribute("data-tnw-polish-wired") === "1") return;
    btn.setAttribute("data-tnw-polish-wired", "1");
    var richEditor = getRichEditorForInput(ta);
    var minWords = parseInt(btn.getAttribute("data-min-words") || "", 10);
    var minChars = parseInt(btn.getAttribute("data-min-chars") || "1", 10);
    if (!Number.isFinite(minChars) || minChars < 1) minChars = 1;
    function currentText() {
      if (richEditor) {
        syncRichEditorToInput(richEditor, false);
        return (richEditor.innerText || "").trim();
      }
      return (ta.value || "").trim();
    }
    function hasEnoughText() {
      var text = currentText();
      if (Number.isFinite(minWords) && minWords > 0) {
        return countWords(text) >= minWords;
      }
      return text.length >= minChars;
    }
    function syncPolishVisibility() {
      var show = hasEnoughText();
      btn.classList.toggle("d-none", !show);
      btn.disabled = !show;
    }
    ta.addEventListener("input", syncPolishVisibility);
    if (richEditor) richEditor.addEventListener("input", syncPolishVisibility);
    syncPolishVisibility();
    btn.addEventListener("click", function () {
      var raw = currentText();
      if (!hasEnoughText()) {
        if (window.showTnwMessage) {
          var needMsg =
            Number.isFinite(minWords) && minWords > 0
              ? "Add at least " + minWords + " words first, then use Suggested Re-Write."
              : "Add at least " + minChars + " characters first, then use Suggested Re-Write.";
          window.showTnwMessage(needMsg, { title: "Description needed" });
        }
        return;
      }
      btn.disabled = true;
      var origHtml = btn.innerHTML;
      btn.innerHTML =
        '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>Working…';
      fetch(polishUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ text: raw }),
      })
        .then(function (r) {
          return r.text().then(function (t) {
            try {
              return { ok: r.ok, status: r.status, body: JSON.parse(t) };
            } catch (_e) {
              return {
                ok: false,
                status: r.status,
                body: {
                  error:
                    "Unexpected response from the server. Try refreshing the page, or sign in again.",
                },
              };
            }
          });
        })
        .then(function (res) {
          if (res.body && res.body.ok && typeof res.body.text === "string") {
            if (richEditor) {
              setRichInputValue(ta, plainTextToRichHtml(res.body.text));
              richEditor.innerHTML = plainTextToRichHtml(res.body.text);
              syncRichEditorToInput(richEditor);
            } else {
              ta.value = res.body.text;
              ta.dispatchEvent(new Event("input", { bubbles: true }));
            }
          } else if (window.showTnwMessage) {
            var msg =
              (res.body && res.body.error) ||
              (res.ok ? "Unexpected response." : "Request failed (" + res.status + ").");
            window.showTnwMessage(msg, { title: "AI polish failed", variant: "danger" });
          }
        })
        .catch(function () {
          if (window.showTnwMessage) {
            window.showTnwMessage("Could not reach the server. Please try again.", {
              title: "Connection problem",
              variant: "danger",
            });
          }
        })
        .finally(function () {
          btn.disabled = false;
          btn.innerHTML = origHtml;
          syncPolishVisibility();
        });
    });
  }

  window.tnwWireRichTextEditors = wireRichTextEditors;
  window.tnwWireGroupDescriptionCopy = wireGroupDescriptionCopy;
  window.tnwWireMeetingDescriptionAiPolish = wireMeetingDescriptionAiPolish;
})();
