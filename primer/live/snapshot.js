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

  // Shown all the way up: no element around it hides it, the opacities multiplied (opacity
  // and filter: opacity()) stay above 0.1, no clip-path, clip or mask hides all of it, the
  // scales multiplied stay at 0.05 or more on each axis, and it has a size. A transparent,
  // clipped-away or shrunk-to-nothing control can't be what an OK was given for.
  function shown(el) {
    var r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    if (typeof el.checkVisibility === "function" &&
        !el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    var alpha = 1, sx = 1, sy = 1;
    for (var e = el; e && e.nodeType === 1; e = e.parentElement || hostOf(e)) {
      var st = window.getComputedStyle(e);
      if (st.display === "none" || st.visibility === "hidden" || st.visibility === "collapse") {
        return false;
      }
      var o = parseFloat(st.opacity);
      alpha *= (isNaN(o) ? 1 : o) * filterAlpha(st.filter);
      if (!(alpha > 0.1)) return false;
      if (clipsAll(e, st) || masksAll(st)) return false;
      var s = scaleOf(st);
      sx *= s[0];
      sy *= s[1];
      if (!(sx >= 0.05 && sy >= 0.05)) return false;
    }
    return true;
  }

  function fraction(v) {  // "0.3" or "30%" -> 0.3; NaN when it isn't a number
    v = String(v).trim();
    return v.slice(-1) === "%" ? parseFloat(v) / 100 : parseFloat(v);
  }

  // what the filter's opacity() functions leave: their amounts multiplied
  function filterAlpha(f) {
    if (!f || f === "none") return 1;
    var a = 1, m, re = /opacity\(\s*([^)\s]*)\s*\)/g;
    while ((m = re.exec(f)) !== null) {
      var v = m[1] === "" ? 1 : fraction(m[1]);
      if (!isNaN(v)) a *= Math.max(0, Math.min(1, v));
    }
    return a;
  }

  function px(v, whole) {  // a computed length or percentage in pixels; NaN for calc() and the like
    v = String(v).trim();
    if (v.slice(-1) === "%") return parseFloat(v) / 100 * whole;
    if (v === "0" || /^-?[\d.]+(e-?\d+)?px$/.test(v)) return parseFloat(v);
    return NaN;
  }

  // a clip-path (inset, circle, ellipse, polygon, xywh) or a legacy clip that leaves no area
  function clipsAll(e, st) {
    var w = e.offsetWidth, h = e.offsetHeight;
    if (typeof w !== "number") {
      var b = e.getBoundingClientRect();
      w = b.width;
      h = b.height;
    }
    var clip = st.clip;
    if (clip && clip !== "auto" && (st.position === "absolute" || st.position === "fixed")) {
      var c = /rect\(([^)]*)\)/.exec(clip);
      if (c) {
        var q = c[1].split(/[\s,]+/).filter(Boolean);
        if (q.length === 4) {
          var top = q[0] === "auto" ? 0 : px(q[0], h), right = q[1] === "auto" ? w : px(q[1], w);
          var bottom = q[2] === "auto" ? h : px(q[2], h), left = q[3] === "auto" ? 0 : px(q[3], w);
          if (bottom <= top || right <= left) return true;
        }
      }
    }
    var cp = st.clipPath;
    if (!cp || cp === "none") return false;
    var m = /^(inset|circle|ellipse|polygon|xywh)\((.*)\)/.exec(cp.trim());
    if (!m) return false;
    var args = m[2];
    if (m[1] === "inset") {
      var t = args.split(" round ")[0].trim().split(/\s+/);
      var it = t[0], ir = t[1] || it, ib = t[2] || it, il = t[3] || ir;
      return px(it, h) + px(ib, h) >= h || px(il, w) + px(ir, w) >= w;
    }
    if (m[1] === "circle" || m[1] === "ellipse") {
      var radii = args.split(" at ")[0].trim().split(/\s+/);
      for (var i = 0; i < (m[1] === "circle" ? 1 : 2) && i < radii.length; i++) {
        if (px(radii[i], i ? h : w) === 0) return true;
      }
      return false;
    }
    if (m[1] === "xywh") {
      var xy = args.split(" round ")[0].trim().split(/\s+/);
      return px(xy[2], w) <= 0 || px(xy[3], h) <= 0;
    }
    // polygon: no area at all, because every point lies on one line (within half a pixel).
    // Never the signed area: a self-crossing shape (a bowtie) sums to 0 and still shows.
    var pts = args.replace(/^\s*(nonzero|evenodd)\s*,/, "").split(",");
    var xs = [], ys = [];
    for (var k = 0; k < pts.length; k++) {
      var p = pts[k].trim().split(/\s+/);
      var x = px(p[0], w), y = px(p[1], h);
      if (isNaN(x) || isNaN(y)) return false;
      xs.push(x);
      ys.push(y);
    }
    var dx = 0, dy = 0, len = 0;
    for (var j = 1; j < xs.length && len < 0.5; j++) {  // the first point apart from the first
      dx = xs[j] - xs[0];
      dy = ys[j] - ys[0];
      len = Math.hypot(dx, dy);
    }
    if (len < 0.5) return true;  // every point in one place
    for (var i2 = 1; i2 < xs.length; i2++) {
      // the point's distance from the line through the first two distinct points
      if (Math.abs(dx * (ys[i2] - ys[0]) - dy * (xs[i2] - xs[0])) / len >= 0.5) return false;
    }
    return true;
  }

  // a mask that hides everything: every layer sized 0, or only gradients whose every colour
  // is fully transparent. An image mask (an icon) is taken as showing the control.
  function masksAll(st) {
    var img = st.getPropertyValue("mask-image") || st.getPropertyValue("-webkit-mask-image");
    if (!img || img === "none") return false;
    var size = st.getPropertyValue("mask-size") || st.getPropertyValue("-webkit-mask-size");
    if (size) {
      var layers = size.split(","), zero = 0;
      for (var i = 0; i < layers.length; i++) {
        var d = layers[i].trim().split(/\s+/);
        if (fraction(d[0]) === 0 || (d.length > 1 && fraction(d[1]) === 0)) zero += 1;
      }
      if (zero === layers.length) return true;
    }
    if (/url\(|image-set\(|element\(|paint\(|cross-fade\(/.test(img) || img.indexOf("gradient(") < 0) {
      return false;
    }
    var colours = img.match(/rgba?\([^)]*\)|transparent/g) || [];
    var rest = img.replace(/rgba?\([^)]*\)|transparent/g, "");
    if (!colours.length || /(color|lab|lch|oklab|oklch|hsla?|hwb)\(/.test(rest)) return false;
    for (var j = 0; j < colours.length; j++) {
      if (colours[j] === "transparent") continue;
      var parts = colours[j].replace(/^rgba?\(|\)$/g, "").split(/[\s,\/]+/).filter(Boolean);
      if (parts.length < 4 || fraction(parts[3]) !== 0) return false;
    }
    return true;
  }

  // this element's own scale on each axis: its transform, its scale property and its zoom
  function scaleOf(st) {
    var sx = 1, sy = 1, t = st.transform;
    var m = t && /^matrix(3d)?\(([^)]*)\)$/.exec(t);
    if (m) {
      var v = m[2].split(",").map(parseFloat);
      if (m[1]) {
        sx = Math.hypot(v[0], v[1], v[2]);
        sy = Math.hypot(v[4], v[5], v[6]);
      } else {
        sx = Math.hypot(v[0], v[1]);
        sy = Math.hypot(v[2], v[3]);
      }
    }
    var s = st.scale;
    if (s && s !== "none") {
      var p = s.trim().split(/\s+/).map(fraction);
      sx *= Math.abs(p[0]);
      sy *= Math.abs(p.length > 1 ? p[1] : p[0]);
    }
    var z = parseFloat(st.zoom);
    if (z > 0) {
      sx *= z;
      sy *= z;
    }
    return [isNaN(sx) ? 1 : sx, isNaN(sy) ? 1 : sy];
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
