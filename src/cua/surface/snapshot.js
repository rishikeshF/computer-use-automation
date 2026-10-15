// Snapshot of one frame: every visible interactive control and piece of text, described the way
// a human operator would (role, name, nearby label, table row/column), in document order.
//
// Runs inside the page via Playwright's frame.evaluate(). Called with { start, maxText } and
// returns { nodes, next }. Each emitted element gets a data-cua-ref attribute so act() can find it
// again; refs are reused while the element lives, and numbered from `start` so they stay unique
// across navigations within one session.
//
// Deliberately simple heuristics, tuned for legacy markup: no <label>, table layouts, no test IDs.
({ start, maxText }) => {
  let next = start;
  const nodes = [];

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const clip = (s) => (s.length > maxText ? s.slice(0, maxText - 1) + "…" : s);

  const visible = (el) => {
    if (el.checkVisibility && !el.checkVisibility({ visibilityProperty: true, checkVisibilityCSS: true })) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  // ---- roles -------------------------------------------------------------------------------
  const INTERACTIVE = new Set([
    "link", "button", "textbox", "searchbox", "spinbutton", "checkbox", "radio",
    "combobox", "listbox", "slider", "tab", "menuitem", "switch",
  ]);
  // Non-interactive roles worth showing as one unit of text.
  const TEXTUAL = new Set(["heading", "alert", "status"]);
  const INPUT_ROLES = {
    button: "button", submit: "button", reset: "button", image: "button",
    checkbox: "checkbox", radio: "radio", range: "slider", number: "spinbutton", search: "searchbox",
  };
  // Controls that need a label to be understood (as opposed to buttons and links, named by text).
  const LABELLABLE = new Set(["textbox", "searchbox", "spinbutton", "checkbox", "radio", "combobox", "listbox", "slider", "switch"]);

  function roleOf(el) {
    const explicit = norm(el.getAttribute("role")).split(" ")[0];
    if (explicit && (INTERACTIVE.has(explicit) || TEXTUAL.has(explicit))) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "hidden") return null;
      return INPUT_ROLES[t] || "textbox"; // text, password, email, tel, url, date, ...
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (/^h[1-6]$/.test(tag)) return "heading";
    return null;
  }

  // ---- names -------------------------------------------------------------------------------
  // A simplified accessible-name computation. Close enough to the browser's for the markup we
  // care about, and identical between discovery and replay, which matters more than precision.
  function nameOf(el) {
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const s = norm(by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "));
      if (s) return s;
    }
    const aria = norm(el.getAttribute("aria-label"));
    if (aria) return aria;

    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") {
      const t = (el.getAttribute("type") || "").toLowerCase();
      if (t === "submit" || t === "button" || t === "reset") {
        return norm(el.value) || (t === "submit" ? "Submit" : t === "reset" ? "Reset" : "");
      }
      if (t === "image") return norm(el.getAttribute("alt")) || norm(el.title) || "Submit";
      if (el.labels && el.labels.length) {
        const s = norm([...el.labels].map((l) => l.innerText).join(" "));
        if (s) return s;
      }
      return norm(el.title) || norm(el.getAttribute("placeholder"));
    }
    // Links, buttons, headings: named by their content, including image alt text.
    const text = norm(el.innerText);
    if (text) return text;
    const alts = norm([...el.querySelectorAll("img[alt]")].map((i) => i.alt).join(" "));
    return alts || norm(el.title);
  }

  // ---- inferred labels (legacy forms) ------------------------------------------------------
  // Legacy pages put "Username" in a <p> or <td> next to the input instead of using <label>.
  // Walk the document from the control to the nearest visible text, stopping if another control
  // comes first (then the text belongs to that control, not this one).
  function isControl(el) {
    const r = roleOf(el);
    return r !== null && INTERACTIVE.has(r);
  }

  function nearestText(el, forward) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    // Going forward, start from the control's last descendant so its own subtree is skipped.
    let startAt = el;
    if (forward) while (startAt.lastChild) startAt = startAt.lastChild;
    walker.currentNode = startAt;
    for (let steps = 0; steps < 200; steps++) {
      if (!(forward ? walker.nextNode() : walker.previousNode())) return null;
      const n = walker.currentNode;
      if (n.nodeType === 1) {
        if (n !== el && !n.contains(el) && isControl(n)) return null;
        continue;
      }
      const p = n.parentElement;
      if (!p || ["SCRIPT", "STYLE", "OPTION", "NOSCRIPT"].includes(p.tagName)) continue;
      const s = norm(n.textContent);
      if (!s || !visible(p)) continue;
      if (p.closest("a[href],button")) return null; // a link's text is not a field label
      if (s.length > 60) return null; // a paragraph, not a label
      return s.replace(/[\s:*]+$/, "") || null; // "Amount: *" -> "Amount"
    }
    return null;
  }

  function inferLabel(el, role) {
    // Checkboxes and radios are usually labelled on the right; everything else on the left/above.
    if (role === "checkbox" || role === "radio") return nearestText(el, true) || nearestText(el, false);
    return nearestText(el, false);
  }

  // ---- tables ------------------------------------------------------------------------------
  function tableOf(el) {
    const cell = el.closest("td,th");
    const row = cell?.parentElement;
    const table = cell?.closest("table");
    if (!cell || !row || !table) return null;
    const idx = [...row.cells].indexOf(cell);
    const head = table.tHead?.rows[0] || [...table.rows].find((r) => [...r.cells].some((c) => c.tagName === "TH"));
    const col = head && head !== row && head.cells[idx] ? clip(norm(head.cells[idx].innerText)) || null : null;
    // The header row has no row context; its first cell is a column title, not a row key.
    const rowText = row !== head && idx > 0 && row.cells[0] ? clip(norm(row.cells[0].innerText)) || null : null;
    return col || rowText ? { col, row: rowText } : null;
  }

  // ---- emit --------------------------------------------------------------------------------
  function emit(el, role, name, extra) {
    if (!el.dataset.cuaRef) el.dataset.cuaRef = "e" + next++;
    const r = el.getBoundingClientRect();
    nodes.push({
      ref: el.dataset.cuaRef,
      role,
      name: clip(name),
      table: tableOf(el),
      bbox: [r.x + window.scrollX, r.y + window.scrollY, r.width, r.height],
      label: null, value: null, options: [], states: [],
      ...extra,
    });
  }

  function controlExtra(el, role, name) {
    const states = [];
    const secret = el.tagName === "INPUT" && (el.type || "").toLowerCase() === "password";
    if (secret) states.push("secret");
    if (el.disabled) states.push("disabled");
    if (el.required) states.push("required");
    if (el.checked) states.push("checked");
    const extra = { states };
    if (!name && LABELLABLE.has(role)) extra.label = inferLabel(el, role);
    if (el.tagName === "SELECT") {
      extra.options = [...el.options].slice(0, 50).map((o) => clip(norm(o.text)));
      extra.value = el.selectedIndex >= 0 ? clip(norm(el.options[el.selectedIndex].text)) : null;
    } else if ((role === "textbox" || role === "searchbox" || role === "spinbutton") && !secret) {
      // Never read the value of a secret field, not even to redact it later.
      extra.value = clip(el.value || "");
    }
    return extra;
  }

  // Elements whose text has already been emitted as one unit (a link, a heading, a table cell).
  const consumed = new Set();
  const insideConsumed = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) if (consumed.has(p)) return true;
    return false;
  };

  for (const el of document.body ? document.body.querySelectorAll("*") : []) {
    if (["SCRIPT", "STYLE", "NOSCRIPT", "OPTION", "TEMPLATE"].includes(el.tagName)) continue;
    if (insideConsumed(el) || !visible(el)) continue;

    const role = roleOf(el);
    if (role && INTERACTIVE.has(role)) {
      const name = nameOf(el);
      emit(el, role, name, controlExtra(el, role, name));
      consumed.add(el);
      continue;
    }
    if (role && TEXTUAL.has(role)) {
      const name = norm(el.innerText);
      if (name) emit(el, role, name, {});
      consumed.add(el);
      continue;
    }
    // A table cell with no controls inside is read as one unit, so "$515.50" keeps its context.
    if ((el.tagName === "TD" || el.tagName === "TH") && !el.querySelector("a[href],button,input,select,textarea")) {
      const name = norm(el.innerText);
      if (name) emit(el, el.tagName === "TH" ? "columnheader" : "cell", name, {});
      consumed.add(el);
      continue;
    }
    // Plain text: the element's own text nodes (not its children's, those are visited next).
    const own = norm([...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" "));
    if (own) emit(el, "text", own, {});
  }

  return { nodes, next };
}
