// Human action capture. Installed in every document while a run can be handed to a person.
//
// Reports each click, selection and edit to Python through the __cuaHuman binding, described
// by role, name and label. A typed value is never read: a fill reports only which field.
(() => {
  if (window.__cuaCaptureOn) return;
  window.__cuaCaptureOn = true;

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 200);
  const BUTTONS = ["button", "submit", "reset", "image"];
  const typeOf = (el) => (el.getAttribute("type") || "").toLowerCase();
  const toggles = (t) => t === "checkbox" || t === "radio";

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const type = typeOf(el);
    let role = { a: "link", button: "button", select: "combobox" }[tag] || "textbox";
    if (tag === "input" && BUTTONS.includes(type)) role = "button";
    if (tag === "input" && toggles(type)) role = type;
    role = el.getAttribute("role") || role;
    const named = role === "button" || role === "link";
    const text = tag === "input" ? el.value : el.innerText;
    const name = norm(el.getAttribute("aria-label")) || (named ? norm(text) : "");
    const label = el.labels && el.labels.length
      ? norm(el.labels[0].innerText)
      : norm(el.getAttribute("name"));
    return { role, name, label };
  }

  function send(kind, el, extra) {
    if (window.__cuaHuman) window.__cuaHuman({ kind, ...describe(el), ...extra });
  }

  document.addEventListener("click", (e) => {
    const sel = "a[href],button,input,[role=button],[role=link]";
    const el = e.target.closest && e.target.closest(sel);
    if (!el) return;
    // Clicking into a text field is not an action; typing in it is (see "change").
    const type = typeOf(el);
    if (el.tagName === "INPUT" && !BUTTONS.includes(type) && !toggles(type)) return;
    send("click", el, {});
  }, true);

  document.addEventListener("change", (e) => {
    const el = e.target;
    if (el.tagName === "SELECT") {
      const chosen = el.selectedOptions[0];
      send("select", el, { option: norm(chosen && chosen.text) });
    } else if (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && !toggles(typeOf(el)))) {
      send("fill", el, {});
    }
  }, true);
})();
