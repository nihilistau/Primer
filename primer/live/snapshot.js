// Primer's live page reader. Runs in an isolated world (the page's own scripts can't replace
// these functions), evaluated as `(<this file>)(op, token, n, arg)`, and returns a JSON string.
//   op "read":   {title, url, text, controls:[{n, kind, label, href?, password?, options?}]},
//                marking each control with data-primer-n="<token>-<n>";
//   op "find":   {ok, kind, label, href?, password?, x, y} for control n (its centre, in CSS
//                pixels of the viewport, after scrolling it into view);
//   op "aim":    "find" plus, in the same step, {clear, moved}: clear when the control and
//                every element around it are shown and the control is what sits at (x, y),
//                so a press there lands on it; moved when bringing it into view moved it;
//   op "insert": in one step, refuse a password field, focus field n and set its value to
//                arg, then fire input and change;
//   op "choose": select the option of list n whose text or value is arg, then fire input and
//                change;
//   op "scroll": scroll the page most of a screen up (arg < 0) or down;
//   op "text":   {title, url, text} with up to arg characters of text, marking nothing.
(function (op, token, n, arg) {
  "use strict";
  var MAX_TEXT = 6000, MAX_CONTROLS = 150, MAX_LABEL = 80, MAX_OPTIONS = 20;
  var ATTR = "data-primer-n";

  // Invisible format characters (bidi overrides and isolates, zero-width marks) and control
  // characters go, so a label reads as what it is.
  var HIDDEN = /\p{Cf}/gu, GAPS = /[\s\p{Cc}]+/gu;

  function clean(s, cap) {
    s = String(s == null ? "" : s).replace(HIDDEN, "").replace(GAPS, " ").trim();
    return s.length > cap ? s.slice(0, cap - 1) + "\u2026" : s;
  }

  function visible(el) {
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    var st = window.getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
  }

  // Shown all the way up: no element around it hides it, the opacities multiplied stay above
  // 0.1, and it has a size. A transparent control can't be what an OK was given for.
  function shown(el) {
    var r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    if (typeof el.checkVisibility === "function" &&
        !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    var alpha = 1;
    for (var e = el; e && e.nodeType === 1; e = e.parentElement || hostOf(e)) {
      var st = window.getComputedStyle(e);
      if (st.display === "none" || st.visibility === "hidden" || st.visibility === "collapse") {
        return false;
      }
      var o = parseFloat(st.opacity);
      alpha *= isNaN(o) ? 1 : o;
      if (!(alpha > 0.1)) return false;
    }
    return true;
  }

  function hostOf(e) {
    var root = e.getRootNode && e.getRootNode();
    return root && root !== document && root.host ? root.host : null;
  }

  // Whether a click at (x, y) lands on el: the element there (looking into open shadow roots)
  // is el or inside it, and isn't a frame or plugin of its own.
  function hits(el, x, y) {
    if (!(x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight)) return false;
    var hit = document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
      var inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    if (!hit) return false;
    if (hit !== el && /^(IFRAME|FRAME|OBJECT|EMBED|FENCEDFRAME)$/.test(hit.tagName)) return false;
    // a frame with pointer-events:none is skipped by elementFromPoint, and the page can turn
    // its pointer events back on in a moment: any shown frame over the point refuses the press
    var frames = document.querySelectorAll("iframe,frame,object,embed,fencedframe");
    for (var i = 0; i < frames.length; i++) {
      var f = frames[i];
      if (f === el || el.contains(f)) continue;
      var r = f.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && x >= r.left && x < r.right && y >= r.top && y < r.bottom) {
        var fs = window.getComputedStyle(f);
        if (fs.display !== "none" && fs.visibility !== "hidden") return false;
      }
    }
    for (var e = hit; e; e = e.parentNode || e.host || null) {
      if (e === el) return true;
    }
    return false;
  }

  // A password field by any sign: its type, its autocomplete hint, or masking by CSS. A
  // "show password" toggle turns type back to text, so the hint and masking count too.
  function isPassword(el) {
    if (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") return false;
    if (el.tagName === "INPUT" && String(el.type).toLowerCase() === "password") return true;
    if (/password/i.test(el.getAttribute("autocomplete") || "")) return true;
    var st = window.getComputedStyle(el);
    var mask = st.getPropertyValue("-webkit-text-security") || st.webkitTextSecurity || "";
    return mask !== "" && mask !== "none";
  }

  function kindOf(el) {
    var tag = el.tagName, role = (el.getAttribute("role") || "").toLowerCase();
    if (tag === "A") return "link";
    if (tag === "SELECT") return "list";
    if (tag === "TEXTAREA") return "field";
    if (tag === "BUTTON") return "button";
    if (tag === "INPUT") {
      var t = String(el.type).toLowerCase();
      if (t === "hidden") return null;
      if (["submit", "button", "reset", "image", "checkbox", "radio"].indexOf(t) >= 0) {
        return "button";
      }
      if (["file", "color", "range"].indexOf(t) >= 0) return null;
      return "field";
    }
    if (role === "link") return "link";
    if (role === "button") return "button";
    return null;
  }

  function labelOf(el) {
    var pw = isPassword(el);
    var aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return clean(aria, MAX_LABEL);
    if (el.labels && el.labels.length) {
      var l = clean(el.labels[0].innerText, MAX_LABEL);
      if (l) return l;
    }
    if (el.tagName !== "INPUT" && el.tagName !== "SELECT" && el.tagName !== "TEXTAREA") {
      var t = clean(el.innerText, MAX_LABEL);
      if (t) return t;
      var img = el.querySelector && el.querySelector("img[alt]");
      if (img && img.alt.trim()) return clean(img.alt, MAX_LABEL);
    }
    var ph = el.getAttribute("placeholder");
    if (ph && ph.trim()) return clean(ph, MAX_LABEL);
    var title = el.getAttribute("title");
    if (title && title.trim()) return clean(title, MAX_LABEL);
    var name = el.getAttribute("name");
    if (name && name.trim()) return clean(name, MAX_LABEL);
    if (pw) return "password field";
    // never the value: it is what someone typed (perhaps a revealed password)
    if (el.tagName === "INPUT" && /^(submit|button|reset)$/i.test(el.type) && el.value) {
      return clean(el.value, MAX_LABEL);  // a button's caption, set by the page
    }
    return "";
  }

  function describe(el, kind) {
    var c = {kind: kind, label: labelOf(el)};
    if (kind === "link") {
      var href = el.href;  // resolved against the page's base
      if (typeof href === "string" && href) c.href = href.slice(0, 2000);
    }
    if (isPassword(el)) {
      c.password = true;
      if (!c.label) c.label = "password field";
    } else if (kind === "field") {
      // whether it already holds text, so the model doesn't type the same thing twice;
      // never the text itself
      c.filled = !!(el.value || el.isContentEditable && el.textContent);
    }
    if (kind === "list") {
      var opts = [];
      for (var i = 0; i < el.options.length && opts.length < MAX_OPTIONS; i++) {
        var o = clean(el.options[i].text, MAX_LABEL);
        if (o) opts.push(o);
      }
      c.options = opts;
    }
    return c;
  }

  function byN(k) {
    return document.querySelector("[" + ATTR + '="' + token + "-" + k + '"]');
  }

  if (op === "read") {
    var old = document.querySelectorAll("[" + ATTR + "]");
    for (var i = 0; i < old.length; i++) old[i].removeAttribute(ATTR);
    var els = document.querySelectorAll(
      "a[href], button, input, select, textarea, [role=button], [role=link]");
    var controls = [], count = 0;
    for (var j = 0; j < els.length && count < MAX_CONTROLS; j++) {
      var el = els[j], kind = kindOf(el);
      if (!kind || el.disabled || !visible(el)) continue;
      var c = describe(el, kind);
      if (!c.label && kind !== "field") continue;
      count += 1;
      c.n = count;
      el.setAttribute(ATTR, token + "-" + count);
      controls.push(c);
    }
    var text = document.body ? document.body.innerText : "";
    return JSON.stringify({
      title: clean(document.title, 300),
      url: String(location.href).slice(0, 2000),
      text: clean(text, MAX_TEXT),
      controls: controls
    });
  }

  // op "text": {title, url, text} for Ask and Make a page. Marks nothing, so a Click through's
  // numbered controls stay as they were. Field values are never part of innerText.
  if (op === "text") {
    var cap = Math.max(1, Math.min(Number(arg) || 0, 200000));
    var body = document.body ? document.body.innerText : "";
    body = String(body).replace(/[ \t\f\v\u00a0]+/g, " ").replace(/\n\s*\n+/g, "\n\n").trim();
    return JSON.stringify({
      title: clean(document.title, 300),
      url: String(location.href).slice(0, 2000),
      text: body.length > cap ? body.slice(0, cap) : body
    });
  }

  if (op === "scroll") {
    var dir = Number(arg) < 0 ? -1 : 1;
    window.scrollBy(0, dir * Math.max(100, window.innerHeight * 0.8));
    return JSON.stringify({ok: true});
  }

  var el2 = byN(n);
  if (!el2 || !el2.isConnected) return JSON.stringify({ok: false});
  var kind2 = kindOf(el2);
  if (!kind2) return JSON.stringify({ok: false});

  if (op === "find" || op === "aim") {
    var r0 = el2.getBoundingClientRect();
    el2.scrollIntoView({block: "center", inline: "center"});
    var r = el2.getBoundingClientRect();
    var out = describe(el2, kind2);
    out.ok = visible(el2);
    out.x = r.left + r.width / 2;
    out.y = r.top + r.height / 2;
    if (op === "aim") {
      // the check and the point in one synchronous step: no page script runs in between
      out.moved = r.left !== r0.left || r.top !== r0.top;
      out.clear = out.ok && shown(el2) && hits(el2, out.x, out.y);
    }
    return JSON.stringify(out);
  }
  if (op === "insert") {
    // the check and the typing in one synchronous step: no page script runs in between
    if (kind2 !== "field" || isPassword(el2) || el2.readOnly) return JSON.stringify({ok: false});
    el2.scrollIntoView({block: "center"});
    el2.focus();
    if (document.activeElement !== el2 || isPassword(el2)) return JSON.stringify({ok: false});
    var proto = el2.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
                                           : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el2, String(arg));  // this world's own setter: the page can't replace it
    el2.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText",
                                               data: String(arg)}));
    el2.dispatchEvent(new Event("change", {bubbles: true}));
    return JSON.stringify({ok: true});
  }
  if (op === "choose") {
    if (kind2 !== "list") return JSON.stringify({ok: false});
    for (var k = 0; k < el2.options.length; k++) {
      var opt = el2.options[k];
      if (clean(opt.text, MAX_LABEL) === arg || opt.value === arg) {
        el2.value = opt.value;
        el2.dispatchEvent(new Event("input", {bubbles: true}));
        el2.dispatchEvent(new Event("change", {bubbles: true}));
        return JSON.stringify({ok: true});
      }
    }
    return JSON.stringify({ok: false});
  }
  return JSON.stringify({ok: false});
})
