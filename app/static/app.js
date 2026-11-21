// Budget front end. Plain JS, no build step. All user text goes through el() / textContent,
// never innerHTML, so descriptions and notes can't inject markup.

const $app = document.getElementById("app");
const $toast = document.getElementById("toast");
const GROUPS = ["Needs", "Fun", "Investing - Long", "Investing - Short"];
const INTENTS = [["want", "Want"], ["need", "Need"], ["necessity", "Necessity"], ["impulse", "Impulse"]];
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

let S = null;            // last /api/state response
let suggestionCache = null;

// ------------------------------------------------------------------ helpers

function el(tag, props, ...kids) {
  const [name, ...classes] = tag.split(".");
  const n = document.createElement(name || "div");
  if (classes.length) n.className = classes.join(" ");
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "class") n.className += (n.className ? " " : "") + v;
    else if (k === "style") n.style.cssText = v; // CSSOM is allowed under our strict CSP; a style attribute isn't
    else if (k === "value") n.value = v;
    else if (k === "checked") n.checked = !!v;
    else if (v === true) n.setAttribute(k, "");
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  month: "M4 5h16v15H4zM4 9h16M9 3v4M15 3v4",
  dash: "M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  plus: "M12 5v14M5 12h14",
  tag: "M3 12V4h8l10 10-8 8L3 12zM7.5 7.5h.01",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  left: "M15 18l-6-6 6-6",
  right: "M9 18l6-6-6-6",
  back: "M19 12H5M12 19l-7-7 7-7",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
};
function logo() {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 64 64");
  s.setAttribute("class", "logo");
  s.setAttribute("aria-hidden", "true");
  const mk = (tag, attrs) => { const n = document.createElementNS(SVG_NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  s.append(mk("rect", { width: 64, height: 64, rx: 14, class: "logo-bg" }),
    mk("rect", { x: 12, y: 19, width: 40, height: 28, rx: 4, fill: "none", class: "logo-fg", "stroke-width": 4 }),
    mk("path", { d: "M13 21l19 14 19-14", fill: "none", class: "logo-fg", "stroke-width": 4, "stroke-linejoin": "round" }));
  return s;
}

function icon(name) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", ICONS[name]);
  s.append(p);
  return s;
}

let fmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
function setCurrency(code) {
  try { fmt = new Intl.NumberFormat(undefined, { style: "currency", currency: code || "USD" }); }
  catch { fmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }); }
}

// Theme: stored on the server per person, cached locally so theme.js can apply it before the page draws.
function applyTheme(theme, accent) {
  const d = document.documentElement;
  d.setAttribute("data-theme", theme || "system");
  d.setAttribute("data-accent", accent || "teal");
  try { localStorage.setItem("budget-theme", JSON.stringify({ theme, accent })); } catch { /* private mode */ }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", getComputedStyle(d).getPropertyValue("--bg").trim() || "#f6f7f5");
}
function money(cents) { return fmt.format((cents || 0) / 100); }
function moneyEl(cents, { signClass = true } = {}) {
  return el("span.num", { class: signClass && cents < 0 ? "neg" : null }, money(cents));
}
function parseMoney(s) {
  if (s === null || s === undefined) return null;
  const clean = String(s).replace(/[$,\s]/g, "");
  if (!/^-?\d*(\.\d{0,2})?$/.test(clean) || clean === "" || clean === "-" || clean === ".") return null;
  return Math.round(parseFloat(clean) * 100);
}
function centsToInput(c) { return (c / 100).toFixed(2); }

function ymLabel(ym, short = false) {
  const [y, m] = ym.split("-").map(Number);
  return short ? `${MONTH_NAMES[m - 1].slice(0, 3)} ${String(y).slice(2)}` : `${MONTH_NAMES[m - 1]} ${y}`;
}
function dateLabel(d) {
  if (!d) return "";
  const [y, m, day] = d.split("-").map(Number);
  return `${MONTH_NAMES[m - 1].slice(0, 3)} ${day}${y !== new Date().getFullYear() ? ", " + y : ""}`;
}
function todayISO() { return S?.today || new Date().toISOString().slice(0, 10); }

function toast(msg, bad = false) {
  $toast.textContent = msg;
  $toast.className = "show" + (bad ? " bad" : "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => ($toast.className = ""), bad ? 4200 : 2200);
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Budget": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401 && path !== "/api/login") {
    S = null;
    if (!isPublicRoute()) renderLogin();
    throw new Error("Please log in");
  }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    let msg = data?.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join(", ");
    throw new Error(msg || `Something went wrong (${r.status})`);
  }
  return data;
}

async function loadState(ym) {
  S = await api("GET", "/api/state" + (ym ? `?ym=${ym}` : ""));
  setCurrency(S.household.currency);
  if (S.user.theme !== loadState.theme || S.user.accent !== loadState.accent) {
    applyTheme(S.user.theme, S.user.accent);
    loadState.theme = S.user.theme; loadState.accent = S.user.accent;
  }
  return S;
}
const catById = (id) => S.categories.find((c) => c.id === id);
const tagById = (id) => S.tags.find((t) => t.id === id);
const openMonths = () => S.months.filter((m) => m.status === "open").map((m) => m.ym);
const latestOpen = () => openMonths().slice(-1)[0];
const monthStatus = (ym) => S.months.find((m) => m.ym === ym)?.status;

function go(hash) { if (location.hash === hash) render(); else location.hash = hash; }

// ------------------------------------------------------------------ chrome

function shell(active, ...content) {
  const link = (href, name, label, key, extra) =>
    el("a", { href, class: (active === key ? "on " : "") + (extra || "") },
      extra === "add-btn" ? el("span.plus", {}, icon(name)) : icon(name), el("span", {}, label));
  return el("div.shell", {},
    el("nav.nav", { "aria-label": "Main" },
      el("a.brand", { href: "#/dash", "aria-label": "Budget home" },
        logo(),
        el("span", {}, el("b", {}, S.household.name), el("small", {}, "Budget"))),
      link(`#/dash/${S.summary.ym}`, "dash", "Dashboard", "dash"),
      link(`#/month/${S.summary.ym}`, "month", "Month", "month"),
      link("#/add", "plus", "Add", "add", "add-btn"),
      link("#/reports", "chart", "Reports", "reports"),
      link("#/trackers", "tag", "Trackers", "trackers"),
      link("#/settings", "gear", "Settings", "settings"),
    ),
    el("main", {}, ...content),
  );
}

function topBar(title, backHref, ...right) {
  return el("header.top", {},
    typeof backHref === "function" ? el("button.icon-btn", { type: "button", onclick: backHref, "aria-label": "Back" }, icon("back"))
      : backHref ? el("a.icon-btn", { href: backHref, "aria-label": "Back" }, icon("back")) : null,
    el("h1", {}, title),
    ...right);
}

function modal(title, body, { onClose } = {}) {
  const bg = el("div.modal-bg", {
    onclick: (e) => { if (e.target === bg) close(); },
  });
  const box = el("div.modal", { role: "dialog", "aria-modal": "true", "aria-label": title }, el("h2", {}, title), body);
  bg.append(box);
  function close() { bg.remove(); document.removeEventListener("keydown", esc); onClose?.(); }
  function esc(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", esc);
  document.body.append(bg);
  box.querySelector("input,select,textarea,button")?.focus();
  return close;
}

function confirmBox(title, text, okLabel, onOk, danger = false) {
  const close = modal(title, el("div.form", {},
    el("p.muted", { style: "margin:0" }, text),
    el("div.actions", {},
      el("button.btn", { onclick: () => close() }, "Cancel"),
      el("button.btn.primary", {
        class: danger ? "danger" : null, onclick: async (e) => {
          e.target.disabled = true;
          try { await onOk(); close(); } catch (err) { toast(err.message, true); e.target.disabled = false; }
        },
      }, okLabel))));
}

// ------------------------------------------------------------------ logged-out screens
// Log in, sign up, confirm email, forgot / reset password, join by invite, privacy.

let CONFIG = null;
async function getConfig() {
  if (!CONFIG) CONFIG = await api("GET", "/api/config").catch(() => ({ signup: false, email: false }));
  return CONFIG;
}

function authPage(title, sub, ...content) {
  $app.replaceChildren(el("div.login", {},
    el("div.login-brand", {}, logo(), el("span", {}, "Budget")),
    el("h1", {}, title),
    sub ? el("p.muted", {}, sub) : null,
    ...content,
    el("p.small.faint.login-foot", {}, el("a", { href: "#/privacy" }, "Privacy"))));
  $app.querySelector("input")?.focus();
}

function authForm(fields, label, onSubmit) {
  const err = el("p.err", { role: "alert" });
  const btn = el("button.btn.primary.block", { type: "submit" }, label);
  return el("form.form", {
    onsubmit: async (e) => {
      e.preventDefault();
      btn.disabled = true; err.textContent = "";
      try { await onSubmit(); } catch (x) { err.textContent = x.message; } finally { btn.disabled = false; }
    },
  }, ...fields, err, btn);
}

const field = (label, input, hint) => el("div.field", {}, el("label", { for: input.id }, label), input, hint ? el("div.small.faint", {}, hint) : null);
let _fid = 0;
const input = (props) => el("input.input", { id: `f${++_fid}`, required: true, ...props });

function afterLogin() {
  CONFIG = null;
  const h = location.hash;
  if (!h || h === "#/" || /^#\/(login|signup|verify|forgot|reset|join)/.test(h)) location.hash = "#/dash";
  else render();
}

async function renderLogin() {
  S = null;
  const cfg = await getConfig();
  if (cfg.first_run) return renderFirstRun();
  const user = input({ autocomplete: "username", autocapitalize: "none" });
  const pass = input({ type: "password", autocomplete: "current-password" });
  authPage("Log in", null,
    authForm([field("Email or username", user), field("Password", pass)], "Log in", async () => {
      try { await api("POST", "/api/login", { username: user.value, password: pass.value }); }
      catch (x) { pass.select(); throw x; }
      afterLogin();
    }),
    el("div.login-links", {},
      el("a", { href: "#/forgot" }, "Forgot password?"),
      cfg.signup ? el("a", { href: "#/signup" }, "Create an account") : null));
}

function renderFirstRun() {
  const code = input({ autocomplete: "off", autocapitalize: "characters", placeholder: "XXXX-XXXX", maxlength: 20 });
  const name = input({ autocomplete: "name", maxlength: 60 });
  const login = input({ autocomplete: "username", autocapitalize: "none", maxlength: 254 });
  const household = el("input.input", { id: `f${++_fid}`, maxlength: 60, placeholder: "e.g. The Smiths" });
  const pass = input({ type: "password", autocomplete: "new-password", minlength: 10 });
  authPage("Welcome! Create the first account", "This server is new. The first account becomes the owner of the first household.",
    authForm([
      field("Setup code", code, "Printed in the server's log when it started (for Docker: docker logs budget)."),
      field("Your name", name), field("Email or username", login, "Used to log in. An email also lets you reset your password later."),
      field("Household name", household, "Optional. You can change it later."),
      field("Password", pass, "At least 10 characters.")], "Create account", async () => {
      await api("POST", "/api/first-run", { code: code.value, name: name.value, login: login.value, password: pass.value, household: household.value });
      afterLogin();
    }));
}

async function renderSignup() {
  const cfg = await getConfig();
  if (!cfg.signup) {
    return authPage("Sign-ups are closed", "This budget app is invite-only right now. Ask someone who uses it to send you an invite link.",
      el("a.btn.block", { href: "#/login" }, "Back to log in"));
  }
  const name = input({ autocomplete: "name", maxlength: 60 });
  const email = input({ type: "email", autocomplete: "email", maxlength: 254 });
  const household = el("input.input", { id: `f${++_fid}`, maxlength: 60, placeholder: "e.g. Sam & Alex" });
  const pass = input({ type: "password", autocomplete: "new-password", minlength: 10 });
  // Hidden from people (and screen readers); bots fill it in.
  const trap = el("input", { name: "website", tabindex: "-1", autocomplete: "off", "aria-hidden": "true", class: "trap" });
  authPage("Create your budget", "Free, private, and just for your household.",
    authForm([field("Your name", name), field("Email", email), field("Household name", household, "Optional. You can change it later."),
      field("Password", pass, "At least 10 characters. A short phrase works well."), trap], "Create account", async () => {
      await api("POST", "/api/signup", { name: name.value, email: email.value, household: household.value, password: pass.value, website: trap.value });
      checkEmailPage(email.value, "Click the link in it to confirm your address, then you're in.");
    }),
    el("p.small.faint", {}, "By signing up you agree to how your data is handled on the ", el("a", { href: "#/privacy" }, "privacy page"), "."),
    el("div.login-links", {}, el("a", { href: "#/login" }, "Already have an account? Log in")));
}

function checkEmailPage(email, what) {
  authPage("Check your email", null,
    el("p", {}, "We sent a link to ", el("strong", {}, email), ". ", what),
    el("p.small.faint", {}, "No email after a few minutes? Check spam, or log in again to get a new link."),
    el("a.btn.block", { href: "#/login" }, "Back to log in"));
}

async function renderVerify(params) {
  authPage("Confirming your email…", null);
  try {
    await api("POST", "/api/verify", { token: params.get("token") || "" });
    toast("Email confirmed");
    location.hash = "#/dash";
  } catch (x) {
    authPage("That link didn't work", x.message, el("a.btn.block", { href: "#/login" }, "Log in to get a new link"));
  }
}

function renderForgot() {
  const email = input({ type: "email", autocomplete: "email" });
  authPage("Reset your password", "Enter the email on your account and we'll send you a link to choose a new password.",
    authForm([field("Email", email)], "Send link", async () => {
      await api("POST", "/api/forgot", { email: email.value });
      checkEmailPage(email.value, "If there's an account with that address, the link is in there. It works for 1 hour.");
    }),
    el("div.login-links", {}, el("a", { href: "#/login" }, "Back to log in")));
}

function renderReset(params) {
  const pass = input({ type: "password", autocomplete: "new-password", minlength: 10 });
  authPage("Choose a new password", "This signs you out everywhere else.",
    authForm([field("New password", pass, "At least 10 characters.")], "Save password", async () => {
      await api("POST", "/api/reset", { token: params.get("token") || "", password: pass.value });
      toast("Password changed");
      location.hash = "#/dash";
    }));
}

async function renderJoin(params) {
  const token = params.get("token") || "";
  let info;
  try { info = await api("GET", `/api/invite?token=${encodeURIComponent(token)}`); }
  catch (x) { return authPage("That invite didn't work", x.message, el("a.btn.block", { href: "#/login" }, "Go to log in")); }
  const name = input({ autocomplete: "name", maxlength: 60 });
  const email = input({ type: "email", autocomplete: "email", value: info.email });
  const pass = input({ type: "password", autocomplete: "new-password", minlength: 10 });
  authPage(`Join ${info.household}`, `${info.invited_by || "Someone"} invited you to share their budget.`,
    authForm([field("Your name", name), field("Email", email), field("Password", pass, "At least 10 characters.")], "Join", async () => {
      const r = await api("POST", "/api/join", { token, name: name.value, email: email.value, password: pass.value });
      if (r.verify) checkEmailPage(email.value, "Click the link in it to confirm your address, then you're in.");
      else afterLogin();
    }),
    el("div.login-links", {}, el("a", { href: "#/login" }, "Already have an account? Log in")));
}

function renderPrivacy() {
  const p = (...t) => el("p", {}, ...t);
  authPage("Privacy", null,
    el("div.prose", {},
      p("This is a small, self-hosted budget app run by one person for friends and family. It isn't a company and there are no ads."),
      el("h2", {}, "What's stored"),
      p("Your name, email, a scrambled (hashed) version of your password, and whatever you put in your budget: categories, entries, notes, bills and income. ",
        "Each household's budget is kept in its own separate file, so households can't see each other's data."),
      el("h2", {}, "Who can see it"),
      p("Everyone in your household, and the person who runs the server. They can technically open the files, the same as anyone hosting a website, ",
        "and only do so to fix problems or restore a backup."),
      el("h2", {}, "What's never done"),
      p("No selling or sharing your data, no tracking or analytics, no third-party scripts. The app doesn't connect to your bank. If you import a bank file, only the rows you choose are saved."),
      el("h2", {}, "Backups and deleting"),
      p("The server keeps rolling backups for about 30 days. A household owner can delete the whole household in Settings → Household; it's removed right away and drops out of backups as they roll over. ",
        "You can download everything as a CSV file at any time from Settings → Account.")),
    el("a.btn.block", { href: S ? "#/dash" : "#/login" }, S ? "Back to the app" : "Back to log in"));
}

const PUBLIC_ROUTES = ["login", "signup", "verify", "forgot", "reset", "join", "privacy"];
function isPublicRoute() { return PUBLIC_ROUTES.includes(location.hash.replace(/^#\//, "").split(/[/?]/)[0]); }

// ------------------------------------------------------------------ first-run setup

const STARTER = [
  ["Needs", [["Rent / mortgage", 150000], ["Utilities", 25000], ["Groceries", 60000], ["Gas / transit", 20000], ["Insurance", 20000], ["Phone & internet", 12000], ["Subscriptions", 4000]]],
  ["Fun", [["Eating out", 20000], ["Fun money", 15000], ["Gifts", 5000]]],
  ["Investing - Short", [["Car maintenance", 10000], ["Home repairs", 10000], ["Medical", 5000]]],
  ["Investing - Long", [["Emergency fund", 20000], ["Retirement (IRA)", 25000, true]]],
];

async function renderSetup() {
  const rows = [];
  const body = el("div", {});
  for (const [grp, cats] of STARTER) {
    body.append(el("div.group-h", {}, el("span", {}, grp)));
    const card = el("div.card.setup-list", {});
    for (const [name, cents, invest] of cats) {
      const on = el("input", { type: "checkbox", checked: true, "aria-label": `Use ${name}` });
      const nm = el("input.input", { value: name, maxlength: 60, "aria-label": "Category name" });
      const amt = el("input.input.num", { value: centsToInput(cents), inputmode: "decimal", "aria-label": `${name} monthly budget` });
      rows.push({ on, nm, amt, grp, invest: !!invest });
      card.append(el("div.setup-row", {}, on, nm, amt));
    }
    body.append(card);
  }
  const income = el("input.input.num", { inputmode: "decimal", placeholder: "e.g. 5000", style: "max-width:180px" });
  const total = el("strong.num", {});
  const update = () => total.replaceChildren(money(rows.filter((r) => r.on.checked).reduce((a, r) => a + (parseMoney(r.amt.value) || 0), 0)));
  body.addEventListener("input", update); update();

  const submit = async (useRows) => {
    const cats = useRows ? rows.filter((r) => r.on.checked && r.nm.value.trim()).map((r) => ({
      name: r.nm.value.trim(), grp: r.grp, budget_cents: parseMoney(r.amt.value) || 0, is_investment: r.invest })) : [];
    try {
      await api("POST", "/api/setup", { categories: cats, income_cents: parseMoney(income.value || "0") || 0 });
      toast("You're all set"); location.hash = useRows ? "#/dash" : "#/settings?tab=categories"; render();
    } catch (x) { toast(x.message, true); }
  };
  $app.replaceChildren(el("main.setup", {},
    el("div.login-brand", {}, logo(), el("span", {}, S.household.name)),
    el("h1", {}, "Set up your budget"),
    el("p.muted", {}, "This is envelope budgeting: each category gets money every month, and whatever you don't spend rolls over so you can save up. ",
      "Start from these and change anything. You can rename, add or remove categories any time in Settings."),
    el("div.card.pad", { style: "margin-bottom:6px" }, el("div.field", {}, el("label", {}, "Monthly take-home income (optional)"), income),
      el("p.small.faint", { style: "margin:6px 0 0" }, "Used to show how much is left to assign. Budgeted: ", total)),
    body,
    el("div.actions", { style: "margin:18px 0 40px" },
      el("button.btn.primary", { onclick: () => submit(true) }, "Start budgeting"),
      el("button.btn", { onclick: () => submit(false) }, "Start empty instead"))));
}

// Over this month's budget: amber if the category's savings (bank) still cover it, red if the bank is negative too.
function leftClass(left, bank) { return left >= 0 ? null : bank < 0 ? "neg" : "warn-text"; }
const leftTitle = (left, bank) => left < 0 && bank >= 0 ? "Over this month's budget, covered by money saved in this category" : null;

// Close / reopen button for the month being viewed, shown in the header.
// The oldest open month can be closed, including the current one (early close, e.g. on the 29th);
// only the latest closed month can be reopened.
function monthAction() {
  const m = S.summary, thisMonth = todayISO().slice(0, 7);
  if (m.status === "open" && m.ym <= thisMonth && openMonths()[0] === m.ym)
    return el("button.btn.sm" + (m.ym < thisMonth ? ".primary" : ""), { onclick: () => closeMonthDialog(m.ym), title: m.ym < thisMonth ? null : "Close this month before it ends" },
      m.ym < thisMonth ? "Close month" : "Close early");
  if (m.status === "closed" && S.months.filter((x) => x.status === "closed").slice(-1)[0]?.ym === m.ym)
    return el("button.btn.sm", { onclick: () => confirmBox(`Reopen ${ymLabel(m.ym)}?`, "You'll be able to edit its entries and budgets again. Close it again when you're done.", "Reopen",
      async () => { await api("POST", `/api/months/${m.ym}/reopen`); toast("Reopened"); render(); }) }, "Reopen");
  return null;
}

// Month picker with prev/next arrows; `base` is the route to navigate to (e.g. "#/month").
function monthHeader(base, ...right) {
  const m = S.summary, isOpen = m.status === "open";
  const months = S.months.map((x) => x.ym);
  const idx = months.indexOf(m.ym);
  const select = el("select", { "aria-label": "Month", onchange: (e) => go(`${base}/${e.target.value}`) },
    [...S.months].reverse().map((x) => el("option", { value: x.ym, selected: x.ym === m.ym }, ymLabel(x.ym))));
  return el("header.top", { class: right.some(Boolean) ? "has-action" : null },
    el("div.month-switch", {},
      el("button.icon-btn", { "aria-label": "Previous month", disabled: idx <= 0, onclick: () => go(`${base}/${months[idx - 1]}`) }, icon("left")),
      select,
      el("button.icon-btn", { "aria-label": "Next month", disabled: idx >= months.length - 1, onclick: () => go(`${base}/${months[idx + 1]}`) }, icon("right"))),
    el("span.pill", { class: isOpen ? "open" : null }, isOpen ? "Open" : "Closed"),
    ...right);
}

// ------------------------------------------------------------------ dashboard
// The sheet's "quick view" panel: every envelope with spent / left / bank in one table.

async function renderDashboard(ym) {
  await loadState(ym);
  const m = S.summary;
  const thisMonth = todayISO().slice(0, 7);
  const pending = openMonths().filter((x) => x < thisMonth);
  const banner = pending.length ? el("div.banner.info", {},
    el("div.grow", {}, el("strong", {}, `${ymLabel(pending[0])} is still open. `),
      el("span.muted", {}, "Close it once everything has posted.")),
    el("a.btn.sm", { href: `#/month/${pending[0]}` }, "Go to month")) : null;

  const stats = el("div.stats", {},
    stat("Total spent", m.spent),
    stat("Left in budget", m.left),
    stat("The bank", m.bank_total),
    m.status === "open" ? stat("Left to assign", m.unassigned) : stat("Budgeted", m.budgeted));

  // Retired categories only show up while they still hold money or had activity this month.
  const cats = m.categories.filter((c) => !c.archived || c.balance || c.spent);
  const groups = {};
  for (const c of cats) (groups[c.archived ? "Retired" : c.grp || "Other"] ||= []).push(c);
  const order = [...GROUPS, "Other", "Retired"].filter((g) => groups[g]);
  const sum = (arr, k) => arr.reduce((a, c) => a + c[k], 0);
  // Red only where negative means trouble (left / bank); negative "spent" just means money was added.
  const cell = (cents, red = true) => el("td.r.num", { class: red && cents < 0 ? "neg" : null }, money(cents));
  // "Months saved" = bank ÷ monthly budget (the sheet's last column).
  const monthsSaved = (c) => el("td.r.num.opt", {}, c.budget > 0 ? (c.balance / c.budget).toFixed(1) : "");

  const body = [];
  for (const g of order) {
    const gc = groups[g];
    body.push(el("tr.grp", {}, el("td", {}, g), el("td.r.num.opt", {}, money(sum(gc, "budget"))),
      cell(sum(gc, "spent"), false), el("td.r.num", { class: leftClass(sum(gc, "left"), sum(gc, "balance")) }, money(sum(gc, "left"))), cell(sum(gc, "balance")), el("td.opt", {})));
    for (const c of gc) {
      body.push(el("tr.click", { onclick: () => go(`#/cat/${c.id}/${m.ym}`) },
        el("td", {}, el("a", { href: `#/cat/${c.id}/${m.ym}` }, c.name)),
        el("td.r.num.opt", {}, money(c.budget)),
        cell(c.spent, false), el("td.r.num", { class: leftClass(c.left, c.balance), title: leftTitle(c.left, c.balance) }, money(c.left)), cell(c.balance), monthsSaved(c)));
    }
  }
  const table = el("div.card.table-wrap", {}, el("table.t.dash", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Category"), el("th.r.opt", {}, "Budget"), el("th.r", {}, "Spent"),
      el("th.r", {}, "Left"), el("th.r", {}, "Bank"), el("th.r.opt", { title: "Bank ÷ monthly budget" }, "Months"))),
    el("tbody", {}, body),
    el("tfoot", {}, el("tr", {}, el("td", {}, "Total"), el("td.r.num.opt", {}, money(sum(cats, "budget"))),
      cell(sum(cats, "spent"), false), el("td.r.num", { class: leftClass(sum(cats, "left"), sum(cats, "balance")) }, money(sum(cats, "left"))), cell(sum(cats, "balance")), el("td.opt", {})))));

  $app.replaceChildren(shell("dash",
    monthHeader("#/dash", monthAction()),
    banner, stats, table,
    el("p.small.faint", {}, "Tap a category for its entries and history. Months = how many months of budget the bank holds.")));
}

// ------------------------------------------------------------------ month view

async function renderMonth(ym) {
  await loadState(ym);
  const m = S.summary;
  const isOpen = m.status === "open";
  const thisMonth = todayISO().slice(0, 7);

  const header = monthHeader("#/month", monthAction());

  // Banner: an earlier month still waiting to be closed.
  const pending = openMonths().filter((x) => x < thisMonth);
  let banner = null;
  if (pending.length) {
    const first = pending[0];
    banner = el("div.banner.info", {},
      el("div.grow", {}, el("strong", {}, `${ymLabel(first)} is still open. `),
        el("span.muted", {}, "Close it once everything has posted to lock in its leftovers.")),
      el("button.btn.sm", { onclick: () => closeMonthDialog(first) }, `Review & close ${ymLabel(first, true)}`));
  }

  const stats = el("div.stats", {},
    stat("Left this month", m.left),
    stat("In the bank", m.bank_total),
    stat("Spent (net)", m.spent),
    isOpen ? stat("Left to assign", m.unassigned, "of " + money(m.income) + " income") : stat("Budgeted", m.budgeted));

  // Category list grouped like the sheet.
  const groups = {};
  for (const c of m.categories) (groups[c.archived ? "Retired" : c.grp || "Other"] ||= []).push(c);
  const order = [...GROUPS, "Other", "Retired"].filter((g) => groups[g]);
  const lists = order.map((g) => {
    const cats = groups[g];
    const left = cats.reduce((a, c) => a + c.left, 0);
    return [
      el("div.group-h", {}, el("span", {}, g), el("span.num", {}, money(left) + " left")),
      el("div.card.cat-list", {}, cats.map(catRow)),
    ];
  });

  function catRow(c) {
    const pct = c.budget > 0 ? Math.min(100, Math.max(0, (c.spent / c.budget) * 100)) : (c.spent > 0 ? 100 : 0);
    const over = c.left < 0;
    return el("a.cat", { href: `#/cat/${c.id}/${m.ym}` },
      el("div.name", {}, c.name),
      el("div.left.num", { class: leftClass(c.left, c.balance), title: leftTitle(c.left, c.balance) }, money(c.left)),
      el("div.bar", { class: over ? "over" : null }, el("i", { style: `width:${pct.toFixed(1)}%` })),
      el("div.sub", {}, `${money(c.spent)} of ${money(c.budget)}`),
      el("div.sub.r", {}, "bank ", el("span.num", { class: c.balance < 0 ? "neg" : null }, money(c.balance))));
  }

  // Entries for the month.
  const txWrap = el("div.card.tx-list", {}, el("div.empty", {}, "Loading…"));
  const search = el("input.input", { type: "search", placeholder: "Search all entries", "aria-label": "Search all entries", style: "max-width:260px" });
  search.addEventListener("keydown", (e) => { if (e.key === "Enter" && search.value.trim()) go(`#/search/${encodeURIComponent(search.value.trim())}`); });

  $app.replaceChildren(shell("month",
    header, banner, stats, ...lists,
    el("div.section-h", {}, el("h2", {}, "Entries"), el("div.actions", {}, search, isOpen ? el("a.btn.sm", { href: `#/batch?ym=${m.ym}` }, "Add several") : null)),
    txWrap));

  const txs = await api("GET", `/api/transactions?ym=${m.ym}`);
  txWrap.replaceChildren(...(txs.length ? txs.map((t) => txRow(t, { showCategory: true })) : [el("div.empty", {}, "Nothing logged yet this month.")]));
}

function stat(label, cents, sub) {
  return el("div.card.stat", {},
    el("div.label", {}, label),
    el("div.value", {}, moneyEl(cents)),
    sub ? el("div.small.faint", {}, sub) : null);
}

function txRow(t, { showCategory = false, showMonth = false } = {}) {
  const locked = t.ym && monthStatus(t.ym) === "closed";
  let desc = t.description || (t.kind === "add" ? "Added money" : "(no description)");
  const metaBits = [];
  if (showMonth && t.ym) metaBits.push(ymLabel(t.ym, true));
  if (t.date) metaBits.push(dateLabel(t.date));
  if (showCategory && t.category) metaBits.push(t.category);
  if (t.notes) metaBits.push(t.notes);
  const amt = t.kind === "record" ? t.amount_cents : -t.amount_cents; // spending shows as negative
  return el("div.tx", {
    class: locked ? "locked" : null,
    role: "button", tabindex: "0",
    onclick: () => locked ? toast(`${ymLabel(t.ym)} is closed`) : go(`#/edit/${t.id}`),
    onkeydown: (e) => { if (e.key === "Enter") e.currentTarget.click(); },
  },
    el("div.d", {}, desc, t.tag_ids.map((id) => tagById(id) ? el("span.tag-chip", {}, tagById(id).name) : null)),
    el("div.a.num", { class: t.kind === "record" ? null : amt > 0 ? "pos" : null }, (amt > 0 && t.kind !== "record" ? "+" : "") + money(amt)),
    el("div.m", {}, metaBits.join(" · ")),
    el("div.m2", {}, [t.kind === "move" ? "move" : t.kind === "add" ? "added" : null, t.intent, t.entered_by_name, locked ? "🔒" : null].filter(Boolean).join(" · ")));
}

function closeMonthDialog(ym) {
  loadState(ym).then((st) => {
    const s = st.summary;
    const rows = s.categories.filter((c) => c.budget || c.spent);
    const early = ym >= todayISO().slice(0, 7);
    const body = el("div.form", {},
      early ? el("div.banner.info", { style: "margin:0" }, el("div.grow", {}, el("strong", {}, `${ymLabel(ym)} isn't over yet. `),
        `Closing now opens ${ymLabel(ymAdd(ym, 1))}, and anything you add from now on counts toward it. Late charges for ${ymLabel(ym, true)} can still go in if you reopen it.`)) : null,
      el("p.muted", { style: "margin:0" }, `Closing locks ${ymLabel(ym)}: its entries and budgets can't be changed (you can reopen it if needed). These leftovers are already counted in each envelope's bank balance.`),
      el("div.table-wrap", {}, el("table.t", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Category"), el("th.r", {}, "Leftover"), el("th.r", {}, "Bank after"))),
        el("tbody", {}, rows.map((c) => el("tr", {}, el("td", {}, c.name), el("td.r", {}, moneyEl(c.left)), el("td.r", {}, moneyEl(c.balance))))),
        el("tfoot", {}, el("tr", {}, el("td", {}, el("strong", {}, "Total")), el("td.r", {}, moneyEl(s.left)), el("td.r", {}, moneyEl(s.bank_total)))))),
      el("div.actions", {},
        el("button.btn", { onclick: () => close() }, "Not yet"),
        el("button.btn.primary", {
          onclick: async (e) => {
            e.target.disabled = true;
            try { await api("POST", `/api/months/${ym}/close`); close(); toast(`${ymLabel(ym)} closed`); await loadState(); go(`#/month/${latestOpen() || ym}`); }
            catch (x) { toast(x.message, true); e.target.disabled = false; }
          },
        }, `Close ${ymLabel(ym, true)}`)));
    const close = modal(`Close ${ymLabel(ym)}`, body);
  }).catch((x) => toast(x.message, true));
}

// ------------------------------------------------------------------ category view

async function renderCategory(id, ym) {
  await loadState(ym);
  const c = S.summary.categories.find((x) => x.id === id);
  if (!c) return go(`#/month/${ym}`);
  const isOpen = S.summary.status === "open";
  const budgetInput = el("input.input.num", { value: centsToInput(c.budget), inputmode: "decimal", style: "max-width:140px", disabled: !isOpen, "aria-label": "Budget this month" });
  const saveBudget = async () => {
    const v = parseMoney(budgetInput.value);
    if (v === null || v < 0) { toast("Enter a valid amount", true); return; }
    if (v === c.budget) return;
    try { await api("PUT", `/api/months/${ym}/budgets/${id}`, { amount_cents: v }); toast("Budget updated for " + ymLabel(ym, true)); render(); }
    catch (x) { toast(x.message, true); }
  };
  budgetInput.addEventListener("change", saveBudget);

  const histWrap = el("div.card.pad", {}, el("div.faint.small", {}, "Loading history…"));
  const txWrap = el("div.card.tx-list", {}, el("div.empty", {}, "Loading…"));

  $app.replaceChildren(shell("month",
    topBar(c.name, `#/month/${ym}`, el("span.pill", { class: isOpen ? "open" : null }, ymLabel(ym, true))),
    el("div.stats", {},
      el("div.card.stat", {}, el("div.label", {}, "Budget this month"), isOpen ? budgetInput : el("div.value", {}, moneyEl(c.budget)),
        c.budget_from_bills ? el("div.small.faint", {}, "Set from your bills list") : null),
      stat("Spent (net)", c.spent), stat("Left this month", c.left), stat("In the bank", c.balance)),
    isOpen ? el("div.actions", { style: "margin-bottom:14px" },
      el("a.btn.primary", { href: `#/add?cat=${id}&ym=${ym}` }, "Add entry"),
      el("a.btn", { href: `#/add?kind=move&to=${id}&ym=${ym}` }, "Move money in"),
      el("a.btn", { href: `#/add?kind=move&from=${id}&ym=${ym}` }, "Move money out")) : null,
    el("div.section-h", {}, el("h2", {}, "Last 12 months")), histWrap,
    el("div.section-h", {}, el("h2", {}, `Entries in ${ymLabel(ym)}`)), txWrap));

  const [hist, txs] = await Promise.all([api("GET", `/api/history/${id}`), api("GET", `/api/transactions?ym=${ym}&category_id=${id}`)]);
  txWrap.replaceChildren(...(txs.length ? txs.map((t) => txRow(t)) : [el("div.empty", {}, "No entries.")]));

  const last = hist.filter((h) => h.ym <= ym).slice(-12);
  if (!last.length) { histWrap.replaceChildren(el("div.faint", {}, "No history.")); return; }
  const max = Math.max(1, ...last.map((h) => Math.max(h.spent, h.budget)));
  histWrap.replaceChildren(
    el("div.hist", { role: "img", "aria-label": "Spending vs budget, last 12 months" },
      last.map((h) => el("div.col", { title: `${ymLabel(h.ym)}: spent ${money(h.spent)} of ${money(h.budget)}` },
        el("b", { style: `bottom:${(h.budget / max) * 100}%` }),
        el("i", { class: h.spent > h.budget ? "over" : null, style: `height:${(Math.max(0, h.spent) / max) * 100}%` })))),
    el("div.hist-x", {}, last.map((h) => el("span", {}, ymLabel(h.ym, true).split(" ")[0]))),
    el("div.table-wrap", { style: "margin-top:10px" }, el("table.t", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Month"), el("th.r", {}, "Budget"), el("th.r", {}, "Spent"), el("th.r", {}, "Bank"))),
      el("tbody", {}, [...last].reverse().map((h) => el("tr.click", { onclick: () => go(`#/cat/${id}/${h.ym}`) },
        el("td", {}, ymLabel(h.ym, true)), el("td.r", {}, moneyEl(h.budget)), el("td.r", {}, moneyEl(h.spent)), el("td.r", {}, moneyEl(h.balance))))))));
}

// ------------------------------------------------------------------ add / edit

async function getSuggestions() {
  if (!suggestionCache) suggestionCache = await api("GET", "/api/suggestions").catch(() => null);
  return suggestionCache || [];
}

async function renderEntryForm(params, editId) {
  await loadState(params.get("ym") || undefined);
  let existing = null;
  if (editId) {
    existing = await fetchTx(editId);
    if (!existing) { toast("That entry no longer exists", true); return go("#/month"); }
  }
  const opens = openMonths();
  if (!opens.length && !existing) { toast("No open month", true); return go("#/month"); }

  const st = {
    kind: existing ? existing.kind : (params.get("kind") || "spend"),
    cat: existing?.category_id ?? (params.get("cat") ? +params.get("cat") : null),
    from: params.get("from") ? +params.get("from") : null,
    to: params.get("to") ? +params.get("to") : null,
    intent: existing?.intent || null,
    tags: new Set(existing?.tag_ids || (params.get("tag") ? [+params.get("tag")] : [])),
    monthTouched: !!params.get("ym") || !!existing,
  };
  if (existing?.kind === "move") {
    const other = await fetchMoveOther(existing);
    if (existing.amount_cents > 0) { st.from = existing.category_id; st.to = other?.category_id; }
    else { st.to = existing.category_id; st.from = other?.category_id; }
  }
  const isRecord = st.kind === "record";
  const lockedKind = !!existing;

  const amount = el("input.input.amount-input", {
    inputmode: "decimal", placeholder: "0.00", "aria-label": "Amount", autocomplete: "off",
    value: existing ? centsToInput(Math.abs(existing.amount_cents)) : "",
  });
  const desc = el("input.input", { list: "desc-list", placeholder: "e.g. costco", value: existing?.description || "", autocomplete: "off", maxlength: 200 });
  const notes = el("textarea.input", { placeholder: "Optional", maxlength: 1000 }, existing?.notes || "");
  const date = el("input.input", { type: "date", value: existing ? (existing.date || "") : todayISO() });
  const defaultYm = () => {
    const want = (date.value || todayISO()).slice(0, 7);
    return opens.includes(want) ? want : (opens.includes(params.get("ym")) ? params.get("ym") : opens[opens.length - 1]);
  };
  const monthChoices = existing?.ym && !opens.includes(existing.ym) ? [existing.ym, ...opens] : opens;
  const month = el("select.input", { "aria-label": "Counts toward month" },
    monthChoices.map((ym) => el("option", { value: ym }, ymLabel(ym))));
  month.value = existing?.ym || (params.get("ym") && opens.includes(params.get("ym")) ? params.get("ym") : defaultYm());
  month.addEventListener("change", () => (st.monthTouched = true));
  date.addEventListener("change", () => { if (!st.monthTouched) month.value = defaultYm(); });
  const reimbursed = el("input", { type: "checkbox", checked: existing?.reimbursed });
  const err = el("p.err", {});

  const datalist = el("datalist", { id: "desc-list" });
  // Pick the category this description was last used with, unless the user already chose one.
  let sug = [];
  const autoPick = () => {
    const hit = sug.find((s) => s.description.toLowerCase() === desc.value.trim().toLowerCase());
    if (hit && (st.cat === null || st.autoCat) && st.kind !== "move" && hit.category_id !== st.cat) {
      st.cat = hit.category_id; st.autoCat = true; drawCats();
    }
  };
  desc.addEventListener("input", autoPick);
  desc.addEventListener("change", autoPick);
  getSuggestions().then((list) => {
    sug = list;
    datalist.replaceChildren(...sug.map((s) => el("option", { value: s.description })));
    if (!existing) autoPick(); // in case they typed before the list arrived
  });

  const balances = Object.fromEntries(S.summary.categories.map((c) => [c.id, c.left]));
  const activeCats = S.categories.filter((c) => !c.archived || c.id === st.cat || c.id === st.from || c.id === st.to);

  function catChips(selected, onPick) {
    return el("div.chips", {}, activeCats.map((c) => el("button.chip", {
      type: "button", class: selected === c.id ? "on" : null, "aria-pressed": selected === c.id,
      onclick: () => onPick(c.id),
    }, c.name, balances[c.id] !== undefined ? el("span.bal.num", {}, money(balances[c.id])) : null)));
  }

  const catArea = el("div", {});
  function drawCats() {
    if (st.kind === "move" && existing) {
      catArea.replaceChildren(el("p.muted", { style: "margin:0" }, `From ${catById(st.from)?.name ?? "?"} → to ${catById(st.to)?.name ?? "?"}. To change the categories, delete this move and make a new one.`));
    } else if (st.kind === "move") {
      catArea.replaceChildren(
        el("div.field", {}, el("span.label-row", {}, "From"), catChips(st.from, (id) => { st.from = id; drawCats(); })),
        el("div.field", { style: "margin-top:14px" }, el("span.label-row", {}, "To"), catChips(st.to, (id) => { st.to = id; drawCats(); })));
    } else if (st.kind === "record") {
      catArea.replaceChildren();
    } else {
      catArea.replaceChildren(el("div.field", {}, el("span.label-row", {}, "Category"), catChips(st.cat, (id) => { st.cat = id; st.autoCat = false; drawCats(); })));
    }
  }

  const intentChips = el("div.chips", {});
  function drawIntent() {
    intentChips.replaceChildren(...INTENTS.map(([k, label]) => el("button.chip", {
      type: "button", class: st.intent === k ? "on" : null, "aria-pressed": st.intent === k,
      onclick: () => { st.intent = st.intent === k ? null : k; drawIntent(); },
    }, label)));
  }
  const tagChips = el("div.chips", {});
  function drawTags() {
    const tags = S.tags.filter((t) => !t.archived || st.tags.has(t.id));
    tagChips.replaceChildren(...tags.map((t) => el("button.chip", {
      type: "button", class: st.tags.has(t.id) ? "on" : null, "aria-pressed": st.tags.has(t.id),
      onclick: () => { st.tags.has(t.id) ? st.tags.delete(t.id) : st.tags.add(t.id); drawTags(); },
    }, t.name)), el("a.chip", { href: "#/trackers?new=1" }, "+ New tracker"));
  }

  const seg = el("div.seg", { role: "tablist" });
  const descField = el("div.field", {}, el("label", {}, "Description"), desc);
  const extras = el("details.more", { open: !!(existing && (existing.intent || existing.tag_ids.length || existing.notes)) || st.tags.size > 0 },
    el("summary", {}, "More: notes, type, trackers"),
    el("div.form", {},
      el("div.field", {}, el("label", {}, "Notes"), notes),
      el("div.field.intent-field", {}, el("span.label-row", {}, "This is a… (optional)"), intentChips),
      el("div.field.tags-field", {}, el("span.label-row", {}, "Trackers (for taxes or projects)"), tagChips),
      el("label.check", {}, reimbursed, "Reimbursed (HSA etc.)")));
  const monthField = el("div.field", {}, el("label", {}, "Counts toward"), month);

  function drawSeg() {
    const kinds = lockedKind || st.kind === "record" ? [[st.kind, { spend: "Spend", add: "Add money", move: "Move money", record: "Tracking only" }[st.kind]]]
      : [["spend", "Spend"], ["add", "Add money"], ["move", "Move money"]];
    seg.replaceChildren(...kinds.map(([k, label]) => el("button", {
      type: "button", role: "tab", class: st.kind === k ? "on" : null, "aria-selected": st.kind === k,
      onclick: () => { st.kind = k; drawSeg(); drawCats(); layout(); },
    }, label)));
  }
  function layout() {
    descField.classList.toggle("hidden", st.kind === "move");
    monthField.classList.toggle("hidden", st.kind === "record");
    extras.querySelector(".intent-field").classList.toggle("hidden", st.kind !== "spend");
    extras.querySelector(".tags-field").classList.toggle("hidden", st.kind === "move");
    reimbursed.parentElement.classList.toggle("hidden", st.kind === "move");
    desc.placeholder = st.kind === "add" ? "e.g. birthday money from mom" : st.kind === "record" ? "e.g. flights on points" : "e.g. costco";
  }

  const saveBtn = el("button.btn.primary", { type: "submit" }, existing ? "Save" : "Save entry");
  const againBtn = existing ? null : el("button.btn", { type: "button", onclick: () => submit(true) }, "Save & add another");
  const delBtn = existing ? el("button.btn.danger", {
    type: "button", onclick: () => confirmBox("Delete this entry?", existing.kind === "move" ? "Both sides of the move will be removed." : "This can't be undone.", "Delete", async () => {
      await api("DELETE", `/api/entries/${existing.id}`); toast("Deleted"); history.length > 1 ? history.back() : go("#/month");
    }, true),
  }, "Delete") : null;

  async function submit(again = false) {
    err.textContent = "";
    const cents = parseMoney(amount.value);
    if (cents === null || cents === 0 || (cents < 0 && st.kind !== "record")) { err.textContent = "Enter an amount."; amount.focus(); return; }
    const body = { notes: notes.value, date: date.value || null };
    let method = "POST", path;
    try {
      if (st.kind === "move") {
        if (!st.from || !st.to) { err.textContent = "Pick both categories."; return; }
        if (existing) { method = "PATCH"; path = `/api/entries/${existing.id}`; Object.assign(body, { amount_cents: cents, ym: month.value }); }
        else { path = "/api/moves"; Object.assign(body, { ym: month.value, from_id: st.from, to_id: st.to, amount_cents: cents }); }
      } else if (st.kind === "record") {
        Object.assign(body, { amount_cents: cents, description: desc.value, tag_ids: [...st.tags], reimbursed: reimbursed.checked });
        if (existing) { method = "PATCH"; path = `/api/entries/${existing.id}`; } else path = "/api/records";
      } else {
        if (!st.cat) { err.textContent = "Pick a category."; return; }
        Object.assign(body, {
          kind: st.kind, ym: month.value, category_id: st.cat, amount_cents: cents, description: desc.value,
          intent: st.kind === "spend" ? st.intent : null, tag_ids: [...st.tags], reimbursed: reimbursed.checked,
        });
        if (existing) { method = "PATCH"; path = `/api/entries/${existing.id}`; } else path = "/api/entries";
      }
      saveBtn.disabled = true;
      await api(method, path, body);
      suggestionCache = null;
      toast(existing ? "Saved" : "Added " + money(cents));
      if (again) {
        amount.value = ""; desc.value = ""; notes.value = ""; st.cat = null; st.intent = null;
        drawCats(); drawIntent(); amount.focus(); saveBtn.disabled = false;
        await loadState(month.value);
        Object.assign(balances, Object.fromEntries(S.summary.categories.map((c) => [c.id, c.left])));
        drawCats();
      } else if (existing || params.get("back")) {
        history.back();
      } else {
        go(`#/month/${st.kind === "record" ? "" : month.value}`);
      }
    } catch (x) { err.textContent = x.message; saveBtn.disabled = false; }
  }

  const title = existing ? (existing.kind === "move" ? "Edit move" : existing.kind === "record" ? "Edit tracking record" : "Edit entry")
    : isRecord ? "Tracking-only record" : "Add";
  $app.replaceChildren(shell("add",
    topBar(title, existing ? () => (history.length > 1 ? history.back() : go("#/month")) : null,
      existing || isRecord ? null : el("a.btn.sm", { href: `#/batch${params.get("ym") ? "?ym=" + params.get("ym") : ""}` }, "Add several")),
    el("form.form.card.pad", { onsubmit: (e) => { e.preventDefault(); submit(); }, style: "max-width:560px" },
      seg,
      el("div.amount-wrap", {}, el("span", {}, "$"), amount),
      descField, datalist, catArea,
      el("div.row2", {}, el("div.field", {}, el("label", {}, "Date"), date), monthField),
      isRecord ? el("p.small.muted", { style: "margin:0" }, "Tracking-only records count toward trackers but not toward any envelope (e.g. something paid with points).") : null,
      extras, err,
      el("div.actions", {}, delBtn, againBtn, saveBtn))));
  if (existing?.kind === "move" && existing.description) desc.value = existing.description;
  drawSeg(); drawCats(); drawIntent(); drawTags(); layout();
  if (!existing) amount.focus();
}

async function fetchTx(id) {
  try { return await api("GET", `/api/entries/${id}`); } catch { return null; }
}
async function fetchMoveOther(t) {
  try { return await api("GET", `/api/entries/${t.id}/other`); } catch { return null; }
}

// ------------------------------------------------------------------ search

async function renderSearch(q) {
  await loadState();
  const wrap = el("div.card.tx-list", {}, el("div.empty", {}, "Searching…"));
  const input = el("input.input", { type: "search", value: q, "aria-label": "Search", style: "flex:1" });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && input.value.trim()) go(`#/search/${encodeURIComponent(input.value.trim())}`); });
  $app.replaceChildren(shell("month", topBar("Search", `#/month`), el("div.actions", { style: "margin-bottom:12px" }, input), wrap));
  const rows = await api("GET", `/api/transactions?q=${encodeURIComponent(q)}`);
  const total = rows.filter((t) => t.kind === "spend").reduce((a, t) => a + t.amount_cents, 0);
  wrap.before(el("p.muted.small", {}, `${rows.length}${rows.length === 2000 ? "+" : ""} entries · ${money(total)} spent`));
  wrap.replaceChildren(...(rows.length ? rows.map((t) => txRow(t, { showCategory: true, showMonth: true })) : [el("div.empty", {}, "No matches.")]));
}

// ------------------------------------------------------------------ trackers

async function renderTrackers(params) {
  await loadState();
  const tags = await api("GET", "/api/tags");
  const list = (arr) => el("div.card.tx-list", {}, arr.length ? arr.map((t) => el("a.cat", { href: `#/tracker/${t.id}` },
    el("div.name", {}, t.name, el("span.tag-chip", {}, t.kind === "tax" ? "taxes" : "project")),
    el("div.left.num", {}, money(t.total)),
    el("div.sub", {}, t.years.map((y) => `${y.year}: ${money(y.total)}`).join(" · ") || "No entries yet"),
    el("div.sub.r", {}, `${t.n} entries`))) : el("div.empty", {}, "None."));
  const active = tags.filter((t) => !t.archived), archived = tags.filter((t) => t.archived);
  $app.replaceChildren(shell("trackers",
    topBar("Trackers", null, el("button.btn.sm.primary", { onclick: () => tagDialog() }, "New tracker")),
    el("p.muted", { style: "margin-top:0" }, "Tag entries to total them up for taxes (house repairs, medical/HSA) or one-off projects like a trip. Tag a purchase from the Add screen under “More”."),
    list(active),
    archived.length ? [el("div.group-h", {}, el("span", {}, "Closed")), list(archived)] : null));
  if (params.get("new")) tagDialog();
}

function tagDialog(tag) {
  const name = el("input.input", { value: tag?.name || "", maxlength: 60, placeholder: "e.g. Bathroom remodel" });
  const kind = el("select.input", {}, el("option", { value: "project" }, "Project / one-off"), el("option", { value: "tax" }, "Taxes / HSA"));
  kind.value = tag?.kind || "project";
  const archived = el("input", { type: "checkbox", checked: tag?.archived });
  const close = modal(tag ? "Edit tracker" : "New tracker", el("form.form", {
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        const body = { name: name.value, kind: kind.value, archived: archived.checked };
        if (tag) await api("PUT", `/api/tags/${tag.id}`, body); else await api("POST", "/api/tags", body);
        close(); toast("Saved"); render();
      } catch (x) { toast(x.message, true); }
    },
  },
    el("div.field", {}, el("label", {}, "Name"), name),
    el("div.field", {}, el("label", {}, "Kind"), kind),
    tag ? el("label.check", {}, archived, "Closed (finished; hidden from the Add screen, totals kept)") : null,
    el("div.actions", {},
      tag ? el("button.btn.danger", { type: "button", style: "margin-right:auto", onclick: () => { close(); deleteTagDialog(tag); } }, "Delete…") : null,
      el("button.btn", { type: "button", onclick: () => close() }, "Cancel"), el("button.btn.primary", { type: "submit" }, "Save"))));
}

function deleteTagDialog(tag) {
  confirmBox(`Delete “${tag.name}”?`,
    `This removes the tracker and its totals. Purchases tagged with it stay in their envelopes and just lose the tag. ` +
    `Tracking-only records that belong only to this tracker are deleted. ` +
    `If you just want it out of the way, close it instead, which keeps the history for taxes.`,
    "Delete tracker", async () => {
      const r = await api("DELETE", `/api/tags/${tag.id}`);
      toast(r.records_deleted ? `Deleted (and ${r.records_deleted} tracking-only records)` : "Deleted");
      go("#/trackers");
    }, true);
}

async function setTagClosed(tag, closed) {
  try {
    await api("PUT", `/api/tags/${tag.id}`, { name: tag.name, kind: tag.kind, archived: closed });
    toast(closed ? "Tracker closed" : "Tracker reopened"); render();
  } catch (x) { toast(x.message, true); }
}

async function renderTracker(id, params) {
  await loadState();
  const tags = await api("GET", "/api/tags");
  const tag = tags.find((t) => t.id === id);
  if (!tag) return go("#/trackers");
  const year = params.get("year") || "";
  const rows = await api("GET", `/api/transactions?tag_id=${id}`);
  const yearOf = (t) => (t.date || t.ym || String(t.year || "")).slice(0, 4) || "No date";
  const shown = year ? rows.filter((t) => yearOf(t) === year) : rows;
  const total = shown.reduce((a, t) => a + (t.kind === "record" ? t.amount_cents : t.amount_cents), 0);
  const reimbursed = shown.filter((t) => t.reimbursed).reduce((a, t) => a + t.amount_cents, 0);
  shown.sort((a, b) => (b.date || b.ym || "").localeCompare(a.date || a.ym || ""));

  const yearChips = el("div.chips", {},
    el("a.chip", { href: `#/tracker/${id}`, class: !year ? "on" : null }, "All years"),
    tag.years.map((y) => el("a.chip", { href: `#/tracker/${id}?year=${y.year}`, class: year === String(y.year) ? "on" : null }, `${y.year}`)));

  $app.replaceChildren(shell("trackers",
    topBar(tag.name, "#/trackers", el("button.btn.sm", { onclick: () => tagDialog(tag) }, "Edit")),
    tag.archived ? el("p.muted", { style: "margin-top:0" }, "This tracker is closed. ",
      el("button.btn.sm", { onclick: () => setTagClosed(tag, false) }, "Reopen")) : null,
    yearChips,
    el("div.stats", { style: "margin-top:12px" },
      stat(year ? `Total ${year}` : "Total", total),
      stat("Entries", 0, null),
      tag.kind === "tax" ? stat("Reimbursed", reimbursed) : null,
      tag.kind === "tax" ? stat("Not reimbursed", total - reimbursed) : null),
    el("div.actions", { style: "margin-bottom:12px" },
      el("a.btn.sm", { href: `/api/tags/${id}/export.csv${year ? "?year=" + year : ""}` }, "Download CSV"),
      el("a.btn.sm", { href: `#/add?kind=record&tag=${id}&back=1` }, "Add tracking-only record"),
      tag.archived ? null : el("button.btn.sm", { onclick: () => setTagClosed(tag, true) }, "Close tracker")),
    el("div.card.tx-list", {}, shown.length ? shown.map((t) => trackerRow(t, tag)) : el("div.empty", {}, "Nothing here yet."))));
  // replace the placeholder entry-count stat
  $app.querySelectorAll(".stat")[1].querySelector(".value").replaceChildren(el("span.num", {}, String(shown.length)));
}

function trackerRow(t, tag) {
  const row = txRow(t, { showCategory: true, showMonth: !t.date });
  if (tag.kind === "tax") {
    const box = el("input", {
      type: "checkbox", checked: t.reimbursed, "aria-label": "Reimbursed",
      onclick: (e) => e.stopPropagation(),
      onchange: async (e) => {
        try { await api("PATCH", `/api/entries/${t.id}`, { reimbursed: e.target.checked }); toast(e.target.checked ? "Marked reimbursed" : "Unmarked"); render(); }
        catch (x) { toast(x.message, true); e.target.checked = !e.target.checked; }
      },
    });
    row.querySelector(".m2").replaceChildren(el("label.check", { onclick: (e) => e.stopPropagation() }, box, "reimbursed"));
  }
  return row;
}

// ------------------------------------------------------------------ add several (weekly catch-up grid)
// Spreadsheet-style: Enter moves down the same column (type all the places, then all the prices),
// Tab moves across. Category fills in from the description. Negative amount = money added.

async function renderBatch(params) {
  await loadState();
  const opens = openMonths();
  if (!opens.length) { toast("No open month", true); return go("#/dash"); }
  const cats = S.categories.filter((c) => !c.archived);
  const sug = await getSuggestions();
  const byDesc = new Map(sug.map((s) => [s.description.toLowerCase(), s.category_id]));
  const COLS = ["desc", "amount", "cat", "date"];

  const month = el("select.input", { "aria-label": "Counts toward", style: "width:auto" }, opens.map((m) => el("option", { value: m }, ymLabel(m))));
  month.value = opens.includes(params.get("ym")) ? params.get("ym") : latestOpen();
  const list = el("datalist", { id: "batch-desc" }, sug.map((s) => el("option", { value: s.description })));
  const body = el("tbody", {});
  const rows = [];
  const total = el("strong.num", {});
  const status = el("span.muted", {});

  function makeRow() {
    const r = {};
    r.desc = el("input.input", { list: "batch-desc", maxlength: 200, placeholder: rows.length ? "" : "e.g. costco", "aria-label": "Description" });
    r.amount = el("input.input.num", { inputmode: "decimal", placeholder: rows.length ? "" : "12.34", "aria-label": "Amount" });
    r.cat = el("select.input", { "aria-label": "Category" }, el("option", { value: "" }, ""), cats.map((c) => el("option", { value: c.id }, c.name)));
    r.date = el("input.input", { type: "date", "aria-label": "Date (optional)" });
    r.note = el("div.small.batch-note", {});
    r.catAuto = true;
    r.desc.addEventListener("input", () => {
      const hit = byDesc.get(r.desc.value.trim().toLowerCase());
      if (hit && (r.catAuto || !r.cat.value)) { r.cat.value = String(hit); r.catAuto = true; }
      grow(); recheck(r);
    });
    r.cat.addEventListener("change", () => { r.catAuto = false; });
    r.amount.addEventListener("input", () => { grow(); updateTotal(); recheck(r); });
    r.date.addEventListener("change", () => recheck(r));
    const del = el("button.icon-btn", { type: "button", "aria-label": "Remove row", tabindex: "-1", onclick: () => {
      if (rows.length === 1) { r.desc.value = r.amount.value = r.date.value = ""; r.cat.value = ""; return; }
      rows.splice(rows.indexOf(r), 1); r.tr.remove(); updateTotal();
    } }, "×");
    r.tr = el("tr", {}, el("td", {}, r.desc, r.note), el("td", {}, r.amount), el("td", {}, r.cat), el("td.opt", {}, r.date), el("td", {}, del));
    for (const k of COLS) r[k].addEventListener("keydown", (e) => keyNav(e, r, k));
    r[COLS[0]].addEventListener("paste", (e) => pasteInto(e, r, "desc"));
    r.amount.addEventListener("paste", (e) => pasteInto(e, r, "amount"));
    rows.push(r); body.append(r.tr);
    return r;
  }
  const filled = (r) => r.desc.value.trim() || r.amount.value.trim();
  // Always keep a few empty rows at the bottom.
  function grow() { while (rows.slice(-3).some(filled) || rows.length < 6) makeRow(); }

  function keyNav(e, r, k) {
    if (e.key !== "Enter" || e.isComposing) return;
    e.preventDefault();
    let i = rows.indexOf(r) + (e.shiftKey ? -1 : 1);
    if (i < 0) return;
    while (i >= rows.length) makeRow();
    rows[i][k].focus();
    rows[i][k].select?.();
  }

  // Paste several lines: fills down. Tab-separated (from a spreadsheet) fills across too.
  function pasteInto(e, r, k) {
    const text = e.clipboardData?.getData("text") || "";
    if (!/[\n\t]/.test(text.trim())) return;
    e.preventDefault();
    const lines = text.replace(/\r/g, "").split("\n").filter((l) => l.trim() !== "");
    let i = rows.indexOf(r);
    for (const line of lines) {
      while (i >= rows.length) makeRow();
      const cells = line.split("\t");
      const start = COLS.indexOf(k);
      cells.forEach((v, j) => {
        const col = COLS[start + j];
        if (col === "desc" || col === "amount") { rows[i][col].value = v.trim(); rows[i][col].dispatchEvent(new Event("input")); }
      });
      i++;
    }
    grow(); updateTotal();
  }

  function updateTotal() {
    const amts = rows.map((r) => parseMoney(r.amount.value) || 0).filter(Boolean);
    const spent = amts.filter((x) => x > 0).reduce((a, x) => a + x, 0);
    const added = -amts.filter((x) => x < 0).reduce((a, x) => a + x, 0);
    status.textContent = `${amts.length} entr${amts.length === 1 ? "y" : "ies"} · `;
    total.textContent = `${money(spent)} spent` + (added ? ` · ${money(added)} added` : "");
  }

  // "Looks already logged" hint, using the same check as bank import (needs a date or the month).
  let pending = new Set(), timer = null;
  function recheck(r) {
    pending.add(r); clearTimeout(timer);
    timer = setTimeout(async () => {
      const batch = [...pending].filter((x) => rows.includes(x)); pending = new Set();
      const ask = batch.map((x) => ({ x, cents: parseMoney(x.amount.value) })).filter((q) => q.cents);
      batch.forEach((x) => { if (!parseMoney(x.amount.value)) x.note.replaceChildren(); });
      if (!ask.length) return;
      try {
        const res = await api("POST", "/api/import/check", { rows: ask.map((q) => ({
          date: q.x.date.value || `${month.value}-15`, description: q.x.desc.value, amount_cents: q.cents })) });
        ask.forEach((q, i) => {
          const d = res[i].duplicate_of;
          // Without a date, only flag an undated or same-month match (the ±4 day window is a guess).
          const hit = d && (q.x.date.value || d.ym === month.value);
          q.x.note.replaceChildren(hit ? el("span.warn-text", {}, `already logged? “${d.description || "no description"}”${d.date ? " on " + dateLabel(d.date) : ""}`) : "");
        });
      } catch { /* hints only */ }
    }, 450);
  }

  async function save(btn) {
    const ready = [], problems = [];
    rows.forEach((r, i) => {
      r.tr.classList.remove("bad");
      if (!filled(r)) return;
      const cents = parseMoney(r.amount.value);
      if (!cents || !r.cat.value) { problems.push(r); r.tr.classList.add("bad"); return; }
      const d = r.date.value || null;
      const ym = d && opens.includes(d.slice(0, 7)) ? d.slice(0, 7) : month.value;
      ready.push({ r, row: { ym, category_id: +r.cat.value, amount_cents: cents, description: r.desc.value.trim(), date: d } });
    });
    if (problems.length) { (parseMoney(problems[0].amount.value) ? problems[0].cat : problems[0].amount).focus(); return toast(`${problems.length} row${problems.length > 1 ? "s need" : " needs"} an amount and a category`, true); }
    if (!ready.length) return toast("Nothing to add yet", true);
    btn.disabled = true;
    try {
      await api("POST", "/api/entries/batch", { rows: ready.map((x) => x.row) });
      suggestionCache = null;
      toast(`Added ${ready.length} entr${ready.length === 1 ? "y" : "ies"}`);
      go(`#/month/${month.value}`);
    } catch (x) {
      const m = /^Row (\d+)/.exec(x.message);
      if (m) ready[+m[1] - 1]?.r.tr.classList.add("bad");
      toast(x.message, true); btn.disabled = false;
    }
  }

  makeRow(); grow();
  const saveBtn = el("button.btn.primary", { type: "button", onclick: (e) => save(e.target) }, "Add all");
  $app.replaceChildren(shell("add",
    topBar("Add several", () => (history.length > 1 ? history.back() : go("#/month")), el("a.btn.sm", { href: "#/add" }, "Just one")),
    el("p.muted", { style: "margin-top:0" }, "Type all the places, then all the prices: ", el("kbd", {}, "Enter"), " moves down a column, ",
      el("kbd", {}, "Tab"), " moves across. A minus amount adds money to a category. You can also paste a list."),
    el("div.actions", { style: "margin-bottom:10px;align-items:center" }, el("label.small.muted", {}, "Counts toward "), month,
      el("span.small.faint", {}, "(rows with a date in another open month go there)")),
    list,
    el("div.card.table-wrap", {}, el("table.t.batch", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Description"), el("th", {}, "Amount"), el("th", {}, "Category"), el("th.opt", {}, "Date (optional)"), el("th", {}))),
      body)),
    el("div.actions.batch-foot", {}, el("span", {}, status, total), saveBtn)));
  updateTotal();
  rows[0].desc.focus();
}

// ------------------------------------------------------------------ reports
// Spending over a month / quarter / year. "Spent" = purchases only: moves between envelopes aren't
// spending, and money added (gifts, bonuses, refunds) is shown on its own. Categories marked
// "Investment" in Settings count as invested, not spent.

function ymAdd(ym, n) {
  let [y, m] = ym.split("-").map(Number);
  m += n; y += Math.floor((m - 1) / 12); m = ((m - 1) % 12 + 12) % 12 + 1;
  return `${y}-${String(m).padStart(2, "0")}`;
}
function periodRange(kind, p) {  // -> [first ym, last ym]
  if (kind === "year") return [`${p}-01`, `${p}-12`];
  if (kind === "quarter") { const [y, q] = p.split("-Q").map(Number); const s = `${y}-${String(q * 3 - 2).padStart(2, "0")}`; return [s, ymAdd(s, 2)]; }
  return [p, p];
}
function periodOf(kind, ym) {
  const [y, m] = ym.split("-").map(Number);
  return kind === "year" ? String(y) : kind === "quarter" ? `${y}-Q${Math.ceil(m / 3)}` : ym;
}
function periodLabel(kind, p) {
  return kind === "year" ? p : kind === "quarter" ? p.replace(/(\d+)-Q(\d)/, "Q$2 $1") : ymLabel(p);
}
const pctChange = (now, before) => (before > 0 ? (now - before) / before : null);
const monthsBetween = (a, b) => (Number(b.slice(0, 4)) - Number(a.slice(0, 4))) * 12 + Number(b.slice(5)) - Number(a.slice(5)) + 1;

async function renderReports(params) {
  await loadState();
  const thisMonth = todayISO().slice(0, 7);
  const kind = ["month", "quarter", "year"].includes(params.get("by")) ? params.get("by") : "year";
  let p = params.get("p") || periodOf(kind, thisMonth);
  if (!/^\d{4}(-Q[1-4]|-\d{2})?$/.test(p)) p = periodOf(kind, thisMonth);
  const [start, end] = periodRange(kind, p);
  const firstYm = S.months[0]?.ym || start;
  // Only count months that have happened: a period still in progress is compared "so far".
  const lastDone = end < thisMonth ? end : thisMonth;
  // ...and only months the budget has: a year that started in April averages over April onward.
  const effStart = start < firstYm ? firstYm : start;
  const n = Math.max(0, monthsBetween(effStart, lastDone));
  const span = kind === "year" ? 12 : kind === "quarter" ? 3 : 1;
  const prevStart = ymAdd(start, -span), lyStart = ymAdd(start, -12);
  const chartStart = ymAdd(end, -11);
  const fetchStart = [prevStart, lyStart, chartStart].sort()[0];

  const nav = (by, pp) => `#/reports?by=${by}&p=${pp}`;
  const canPrev = ymAdd(start, -1) >= firstYm, canNext = ymAdd(end, 1) <= thisMonth;
  const arrow = (ok, href, label, name) => ok ? el("a.icon-btn", { href, "aria-label": label }, icon(name))
    : el("button.icon-btn", { disabled: true, "aria-label": label }, icon(name));
  const header = el("header.top", {},
    el("div.month-switch", {},
      arrow(canPrev, nav(kind, periodOf(kind, prevStart)), "Previous", "left"),
      el("h1", { style: "flex:none" }, periodLabel(kind, p)),
      arrow(canNext, nav(kind, periodOf(kind, ymAdd(end, 1))), "Next", "right")),
    start <= thisMonth && end >= thisMonth ? el("span.pill.open", {}, "So far") : null);
  const seg = el("div.seg", { style: "margin-bottom:14px;max-width:360px" },
    [["month", "Month"], ["quarter", "Quarter"], ["year", "Year"]].map(([k, l]) =>
      el("button", { type: "button", class: kind === k ? "on" : null, onclick: () => go(nav(k, periodOf(k, lastDone))) }, l)));

  const body = el("div", {}, el("div.empty", {}, "Loading…"));
  $app.replaceChildren(shell("reports", header, seg, body));
  if (n === 0 || start > thisMonth) { body.replaceChildren(el("div.empty", {}, "Nothing here yet.")); return; }

  const data = await api("GET", `/api/reports?start=${fetchStart}&end=${end}`);
  const cat = Object.fromEntries(S.categories.map((c) => [c.id, c]));
  // "Uncategorized" holds old sheet rows that never counted toward any envelope (e.g. travel paid with points).
  data.rows = data.rows.filter((r) => cat[r.category_id]?.name !== "Uncategorized");
  const isSaved = (id) => !!cat[id]?.is_investment;
  const spendOnly = (id) => !isSaved(id);
  const tot = (a, b, key, pred = () => true) =>
    data.rows.reduce((acc, r) => acc + (r.ym >= a && r.ym <= b && pred(r.category_id) ? r[key] : 0), 0);
  // Compare the same stretch of months, one period (or one year) earlier.
  const prevFrom = ymAdd(effStart, -span), prevTo = ymAdd(lastDone, -span);
  const lyFrom = ymAdd(effStart, -12), lyTo = ymAdd(lastDone, -12);

  const spent = tot(effStart, lastDone, "spent", spendOnly);
  const budget = tot(effStart, lastDone, "budget", spendOnly);
  const saved = tot(effStart, lastDone, "spent", isSaved);
  const added = tot(effStart, lastDone, "added");
  const prevSpent = prevFrom >= firstYm ? tot(prevFrom, prevTo, "spent", spendOnly) : null;
  const lySpent = lyFrom >= firstYm ? tot(lyFrom, lyTo, "spent", spendOnly) : null;

  // Per category for this period, plus the previous period for the change column.
  const byCat = {};
  for (const r of data.rows) {
    if (r.ym < start || r.ym > lastDone) continue;
    const t = (byCat[r.category_id] ||= { id: r.category_id, spent: 0, budget: 0, prev: 0 });
    t.spent += r.spent; t.budget += r.budget;
  }
  if (prevSpent !== null) for (const r of data.rows)
    if (r.ym >= prevFrom && r.ym <= prevTo && byCat[r.category_id]) byCat[r.category_id].prev += r.spent;
  const list = Object.values(byCat).filter((t) => t.spent || t.budget);

  // Spending going up is shown in the "bad" color, down in the "good" color, always with an arrow + number.
  const change = (now, before, label) => {
    const c = before === null ? null : pctChange(now, before);
    if (c === null) return el("div.small.faint", {}, `${label}: no data`);
    const up = c > 0;
    return el("div.small", {}, el("span", { class: up ? "neg" : "pos-text" }, `${up ? "▲" : "▼"} ${Math.abs(c * 100).toFixed(0)}%`),
      el("span.faint", {}, ` ${label} (${money(before)})`));
  };
  const tile = (label, cents, ...subs) => el("div.card.stat", {}, el("div.label", {}, label), el("div.value", {}, moneyEl(cents)), ...subs);
  const prevName = kind === "year" ? "vs last year" : kind === "quarter" ? "vs last quarter" : "vs last month";
  const stats = el("div.stats", {},
    tile("Spent", spent, change(spent, prevSpent, prevName), kind !== "year" ? change(spent, lySpent, "vs a year ago") : null),
    // Budget alone understates what was available: added money (bonuses, paybacks, gifts) gets spent too.
    tile("Budgeted", budget, el("div.small.faint", {}, `+ ${money(added)} added (gifts, bonuses, paybacks)`)),
    tile("Invested", saved, el("div.small.faint", {}, S.categories.filter((c) => c.is_investment).map((c) => c.name).join(", ") || "none marked in Settings")),
    n > 1 ? tile("Avg per month", Math.round(spent / n), el("div.small.faint", {}, `spent, over ${n} months`))
      : (() => { const top = list.filter((t) => !isSaved(t.id)).sort((a, b) => b.spent - a.spent)[0];
          return tile(top ? `Most spent: ${cat[top.id]?.name}` : "Most spent", top ? top.spent : 0, el("div.small.faint", {}, "biggest category this month")); })());

  // Monthly bars: the 12 months ending with this period, months inside the period highlighted.
  const months = Array.from({ length: 12 }, (_, i) => ymAdd(chartStart, i));
  const perMonth = months.map((ym) => ({ ym, spent: tot(ym, ym, "spent", spendOnly), budget: tot(ym, ym, "budget", spendOnly), empty: ym > thisMonth || ym < firstYm }));
  const max = Math.max(1, ...perMonth.map((x) => Math.max(x.spent, x.budget)));
  const chart = el("div.card.pad", { style: "margin-bottom:14px" },
    el("div.section-title", {}, el("span", {}, "Spending by month"),
      el("span.legend", {}, el("i.sw"), "spent", el("b.sw"), "budget")),
    el("div.hist.tall", { role: "img", "aria-label": "Spending by month, last 12 months" },
      perMonth.map((x) => el(x.empty ? "div.col" : "a.col", {
        href: x.empty ? null : nav("month", x.ym), class: (x.ym >= start && x.ym <= end ? "in" : "out"),
        title: x.empty ? ymLabel(x.ym) : `${ymLabel(x.ym)}: spent ${money(x.spent)} of ${money(x.budget)} budgeted`,
      },
        x.budget ? el("b", { style: `bottom:${(x.budget / max) * 100}%` }) : null,
        el("i", { style: `height:${(Math.max(0, x.spent) / max) * 100}%` })))),
    el("div.hist-x", {}, perMonth.map((x) => el("span", {}, ymLabel(x.ym, true).split(" ")[0]))));

  const catTable = (rows) => {
    rows.sort((a, b) => b.spent - a.spent);
    const cmax = Math.max(1, ...rows.map((t) => Math.max(t.spent, t.budget)));
    const total = rows.reduce((a, t) => a + t.spent, 0);
    return el("div.card.table-wrap", {}, el("table.t.rep", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Category"), el("th.r", {}, "Spent"), el("th.opt", {}, ""),
        el("th.r.opt", {}, "Budget"), el("th.r", {}, "Share"), el("th.r", {}, prevSpent === null ? "" : "Change"))),
      el("tbody", {}, rows.map((t) => {
        const c = prevSpent === null ? null : pctChange(t.spent, t.prev);
        return el("tr.click", { onclick: () => go(`#/cat/${t.id}/${lastDone}`), title: prevSpent === null ? null : `${prevName}: ${money(t.prev)}` },
          el("td", {}, cat[t.id]?.name || "?"),
          el("td.r.num", {}, money(t.spent)),
          el("td.opt.barcell", {}, el("div.hbar", {},
            el("i", { style: `width:${(Math.max(0, t.spent) / cmax) * 100}%` }),
            t.budget ? el("b", { style: `left:${(t.budget / cmax) * 100}%` }) : null)),
          el("td.r.num.opt", {}, money(t.budget)),
          el("td.r.num.faint", {}, total > 0 ? `${Math.round((t.spent / total) * 100)}%` : ""),
          el("td.r.num", { class: c === null || Math.round(c * 100) === 0 ? "faint" : c > 0 ? "neg" : "pos-text" }, c === null ? "–" : `${c > 0 ? "+" : ""}${(c * 100).toFixed(0)}%`));
      })),
      el("tfoot", {}, el("tr", {}, el("td", {}, "Total"), el("td.r.num", {}, money(total)), el("td.opt", {}),
        el("td.r.num.opt", {}, money(rows.reduce((a, t) => a + t.budget, 0))), el("td", {}), el("td", {})))));
  };

  // Spending by group (Needs / Fun / ...).
  const groups = {};
  for (const t of list) if (!isSaved(t.id)) { const g = cat[t.id]?.grp || "Other"; groups[g] = (groups[g] || 0) + t.spent; }
  const gTotal = Object.values(groups).reduce((a, b) => a + b, 0);
  const gOrder = [...GROUPS, "Other"].filter((g) => groups[g]);
  const groupCard = el("div.card.pad", {},
    el("div.section-title", {}, el("span", {}, "Where it went")),
    gOrder.map((g) => el("div.share-row", {},
      el("span", {}, g),
      el("div.hbar", {}, el("i", { style: `width:${gTotal ? (Math.max(0, groups[g]) / gTotal) * 100 : 0}%` })),
      el("span.num", {}, money(groups[g])),
      el("span.num.faint", {}, gTotal ? `${Math.round((groups[g] / gTotal) * 100)}%` : ""))));

  // Need / want / impulse, when entries are tagged.
  const tagged = data.intents.filter((x) => x.intent);
  const untagged = data.intents.find((x) => !x.intent);
  const INTENT = { need: "Need", necessity: "Necessity", want: "Want", impulse: "Impulse" };
  const intentCard = tagged.length ? el("div.card.pad", {},
    el("div.section-title", {}, el("span", {}, "Need / want / impulse")),
    el("div.chips", {}, tagged.map((x) => el("span.chip", {}, `${INTENT[x.intent] || x.intent}: `, el("span.num", {}, money(x.spent)), el("span.faint", {}, ` (${x.n})`)))),
    untagged ? el("p.small.faint", { style: "margin:8px 0 0" }, `${untagged.n} entries (${money(untagged.spent)}) aren't tagged.`) : null) : null;

  body.replaceChildren(...[
    stats, chart,
    el("div.rep-grid", { class: intentCard ? "two" : null }, groupCard, intentCard),
    el("div.group-h", {}, el("span", {}, "Spending by category")),
    catTable(list.filter((t) => !isSaved(t.id))),
    list.some((t) => isSaved(t.id)) ? [el("div.group-h", {}, el("span", {}, "Invested")), catTable(list.filter((t) => isSaved(t.id)))] : null,
    el("p.small.faint", {}, "Spent counts purchases only (not the old Uncategorized rows): moving money between categories isn't spending, money added (gifts, bonuses, paybacks) is shown separately, and categories marked Investment in Settings count as invested. " +
      "Change compares with the same number of months in the previous period. Tap a month bar or a category to dig in."),
  ].flat().filter(Boolean));
}

// ------------------------------------------------------------------ settings

async function renderSettings(params) {
  await loadState();
  const tab = params.get("tab") || "categories";
  const tabs = el("div.seg.tabs", { style: "margin-bottom:14px;max-width:720px" },
    [["categories", "Categories"], ["bills", "Bills"], ["income", "Income"], ["import", "Import"], ["household", "Household"], ["appearance", "Appearance"], ["account", "Account"]].map(([k, l]) =>
      el("button", { type: "button", class: tab === k ? "on" : null, onclick: () => go(`#/settings?tab=${k}`) }, l)));
  const body = el("div", {});
  $app.replaceChildren(shell("settings", topBar("Settings"), tabs, body));
  if (tab === "categories") await settingsCategories(body);
  else if (tab === "bills") await settingsBills(body);
  else if (tab === "income") await settingsIncome(body);
  else if (tab === "import") await renderImport(body);
  else if (tab === "household") await settingsHousehold(body);
  else if (tab === "appearance") settingsAppearance(body);
  else settingsAccount(body);
}

async function settingsCategories(body) {
  const cats = S.categories;
  const save = async (id, patch) => {
    try { await api("PATCH", `/api/categories/${id}`, patch); toast("Saved"); await loadState(); render(); }
    catch (x) { toast(x.message, true); render(); }
  };
  const active = cats.filter((c) => !c.archived), archived = cats.filter((c) => c.archived);
  const row = (c, i, arr) => {
    const budget = el("input.input.num", { value: centsToInput(c.default_budget_cents), inputmode: "decimal", disabled: !!c.budget_from_bills || !!c.archived, style: "width:110px", "aria-label": `${c.name} monthly budget` });
    budget.addEventListener("change", () => { const v = parseMoney(budget.value); if (v === null || v < 0) return toast("Enter a valid amount", true); save(c.id, { default_budget_cents: v }); });
    const name = el("input.input", { value: c.name, maxlength: 60, style: "min-width:130px", "aria-label": "Name" });
    name.addEventListener("change", () => name.value.trim() && save(c.id, { name: name.value }));
    const grp = el("select.input", { "aria-label": "Group", style: "min-width:120px" }, el("option", { value: "" }, "—"), GROUPS.map((g) => el("option", { value: g }, g)));
    grp.value = c.grp || "";
    grp.addEventListener("change", () => save(c.id, { grp: grp.value || null }));
    const swap = (j) => async () => {
      const other = arr[j];
      await api("PATCH", `/api/categories/${c.id}`, { sort: other.sort });
      await api("PATCH", `/api/categories/${other.id}`, { sort: c.sort === other.sort ? c.sort + (j > i ? 1 : -1) : c.sort });
      render();
    };
    return el("tr", {},
      el("td", {}, name),
      el("td", {}, grp),
      el("td.r", {}, budget, c.budget_from_bills ? el("div.small.faint", {}, "from bills") : null),
      el("td", {}, el("label.check", { title: "Reports count money in this category as invested, not spent" },
        el("input", { type: "checkbox", checked: !!c.is_investment, onchange: (e) => save(c.id, { is_investment: e.target.checked }) }), "Investment")),
      el("td", {}, el("div.actions", { style: "flex-wrap:nowrap;gap:4px" },
        c.archived ? null : el("button.icon-btn", { "aria-label": "Move up", disabled: i === 0, onclick: swap(i - 1) }, "↑"),
        c.archived ? null : el("button.icon-btn", { "aria-label": "Move down", disabled: i === arr.length - 1, onclick: swap(i + 1) }, "↓"),
        el("button.btn.sm", { onclick: () => c.archived ? save(c.id, { archived: false }) : confirmBox(`Retire ${c.name}?`, "It stops getting a budget in open and future months. Its history and bank balance stay. You can bring it back later.", "Retire", () => save(c.id, { archived: true })) }, c.archived ? "Restore" : "Retire"))));
  };
  const newName = el("input.input", { placeholder: "New category", maxlength: 60 });
  const newGrp = el("select.input", {}, GROUPS.map((g) => el("option", { value: g }, g)));
  const newBudget = el("input.input.num", { placeholder: "Monthly budget", inputmode: "decimal" });
  body.append(
    el("p.muted", { style: "margin-top:0" }, "Monthly budgets here are the defaults. Changing one updates every open month and all future months; closed months keep what they had. To change just one month, open the category from the Month screen. Tick “Investment” for categories that are real investments (like a Roth IRA) so Reports count them as invested instead of spent."),
    el("div.card.table-wrap", {}, el("table.t", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Group"), el("th.r", {}, "Monthly"), el("th", { title: "Reports count it as invested, not spent" }, "Reports"), el("th", {}))),
      el("tbody", {}, active.map(row)))),
    el("form.card.pad.actions", {
      style: "margin-top:12px;align-items:flex-end",
      onsubmit: async (e) => {
        e.preventDefault();
        const v = parseMoney(newBudget.value || "0");
        if (!newName.value.trim() || v === null) return toast("Enter a name and amount", true);
        try { await api("POST", "/api/categories", { name: newName.value, grp: newGrp.value, default_budget_cents: v }); toast("Added"); render(); }
        catch (x) { toast(x.message, true); }
      },
    }, newName, newGrp, newBudget, el("button.btn.primary", { type: "submit" }, "Add category")),
    archived.length ? [el("div.group-h", {}, el("span", {}, "Retired")), el("div.card.table-wrap", {}, el("table.t", {}, el("tbody", {}, archived.map(row))))] : null);
}

async function settingsBills(body) {
  const bills = await api("GET", "/api/bills");
  const perMonth = (b) => b.frequency === "monthly" ? b.amount_cents : b.frequency === "quarterly" ? b.amount_cents / 3 : b.amount_cents / 12;
  const billCats = S.categories.filter((c) => c.budget_from_bills);
  const groups = [...billCats.map((c) => [c, bills.filter((b) => b.category_id === c.id)]),
    [null, bills.filter((b) => !billCats.some((c) => c.id === b.category_id))]];
  body.append(el("p.muted", { style: "margin-top:0" }, "Bills feed the budget of the category they belong to (quarterly ÷ 3, yearly ÷ 12). Inactive bills stay listed but don't count."));
  for (const [cat, list] of groups) {
    if (!cat && !list.length) continue;
    const total = list.filter((b) => b.active).reduce((a, b) => a + perMonth(b), 0);
    body.append(
      el("div.group-h", {}, el("span", {}, cat ? cat.name : "Not linked to a category"), el("span.num", {}, money(Math.round(total)) + " / month")),
      el("div.card.table-wrap", {}, el("table.t", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Bill"), el("th.r", {}, "Amount"), el("th.r", {}, "Per month"))),
        el("tbody", {}, list.length ? list.map((b) => el("tr.click", { onclick: () => billDialog(b), style: b.active ? null : "opacity:.5" },
          el("td", {}, b.name, el("div.small.faint", {},
            [b.frequency, b.renews && `renews ${b.renews}`, b.notes, !b.active && "inactive"].filter(Boolean).join(" · "))),
          el("td.r", {}, moneyEl(b.amount_cents)),
          el("td.r", {}, moneyEl(Math.round(perMonth(b)))))) : el("tr", {}, el("td.faint", { colspan: 3 }, "No bills"))))));
  }
  body.append(el("div.actions", { style: "margin-top:12px" }, el("button.btn.primary", { onclick: () => billDialog() }, "Add bill")));
}

function billDialog(b) {
  const name = el("input.input", { value: b?.name || "", maxlength: 80 });
  const amount = el("input.input.num", { value: b ? centsToInput(b.amount_cents) : "", inputmode: "decimal" });
  const freq = el("select.input", {}, ["monthly", "quarterly", "yearly"].map((f) => el("option", { value: f }, f)));
  freq.value = b?.frequency || "monthly";
  const renews = el("input.input", { value: b?.renews || "", maxlength: 40, placeholder: "e.g. Dec" });
  const cat = el("select.input", {}, el("option", { value: "" }, "—"), S.categories.filter((c) => !c.archived).map((c) => el("option", { value: c.id }, c.name + (c.budget_from_bills ? " (from bills)" : ""))));
  cat.value = b?.category_id ?? S.categories.find((c) => c.budget_from_bills)?.id ?? "";
  const notes = el("input.input", { value: b?.notes || "", maxlength: 500 });
  const active = el("input", { type: "checkbox", checked: b ? !!b.active : true });
  const close = modal(b ? "Edit bill" : "Add bill", el("form.form", {
    onsubmit: async (e) => {
      e.preventDefault();
      const v = parseMoney(amount.value);
      if (!name.value.trim() || v === null || v < 0) return toast("Enter a name and amount", true);
      const payload = { name: name.value, amount_cents: v, frequency: freq.value, renews: renews.value, category_id: cat.value ? +cat.value : null, notes: notes.value, active: active.checked };
      try {
        if (b) await api("PUT", `/api/bills/${b.id}`, payload); else await api("POST", "/api/bills", payload);
        close(); toast("Saved"); render();
      } catch (x) { toast(x.message, true); }
    },
  },
    el("div.field", {}, el("label", {}, "Name"), name),
    el("div.row2", {}, el("div.field", {}, el("label", {}, "Amount"), amount), el("div.field", {}, el("label", {}, "Every"), freq)),
    el("div.row2", {}, el("div.field", {}, el("label", {}, "Renews"), renews), el("div.field", {}, el("label", {}, "Category"), cat)),
    el("div.field", {}, el("label", {}, "Notes"), notes),
    el("label.check", {}, active, "Active (counts toward the budget)"),
    el("div.actions", {},
      b ? el("button.btn.danger", { type: "button", onclick: () => confirmBox("Delete this bill?", "Tip: untick Active instead to keep it on the list.", "Delete", async () => { await api("DELETE", `/api/bills/${b.id}`); close(); render(); }, true) }, "Delete") : null,
      el("button.btn", { type: "button", onclick: () => close() }, "Cancel"),
      el("button.btn.primary", { type: "submit" }, "Save"))));
}

async function settingsIncome(body) {
  const inc = await api("GET", "/api/income");
  const total = inc.filter((i) => i.active).reduce((a, i) => a + i.amount_cents, 0);
  const row = (i) => {
    const name = el("input.input", { value: i?.name || "", placeholder: "e.g. Paycheck", maxlength: 80 });
    const amt = el("input.input.num", { value: i ? centsToInput(i.amount_cents) : "", inputmode: "decimal", placeholder: "Monthly", style: "width:130px" });
    const saveIt = async () => {
      const v = parseMoney(amt.value);
      if (!name.value.trim() || v === null) return;
      try {
        if (i) await api("PUT", `/api/income/${i.id}`, { name: name.value, amount_cents: v, active: true });
        else await api("POST", "/api/income", { name: name.value, amount_cents: v, active: true });
        toast("Saved"); render();
      } catch (x) { toast(x.message, true); }
    };
    if (i) { name.addEventListener("change", saveIt); amt.addEventListener("change", saveIt); }
    return el("tr", {}, el("td", {}, name), el("td.r", {}, amt),
      el("td", {}, i ? el("button.btn.sm.danger", { onclick: () => confirmBox(`Remove ${i.name}?`, "", "Remove", async () => { await api("DELETE", `/api/income/${i.id}`); render(); }, true) }, "Remove")
        : el("button.btn.sm.primary", { onclick: saveIt }, "Add")));
  };
  body.append(
    el("p.muted", { style: "margin-top:0" }, "Monthly take-home income. The Month screen compares it to what you've budgeted (“Left to assign”)."),
    el("div.card.table-wrap", {}, el("table.t", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Source"), el("th.r", {}, "Per month"), el("th", {}))),
      el("tbody", {}, inc.map(row), row(null)),
      el("tfoot", {}, el("tr", {}, el("td", {}, el("strong", {}, "Total")), el("td.r", {}, moneyEl(total)), el("td", {}))))));
}

async function settingsHousehold(body) {
  const h = await api("GET", "/api/household");
  const isOwner = h.role === "owner";
  const name = el("input.input", { value: h.name, maxlength: 60, disabled: !isOwner, "aria-label": "Household name" });
  name.addEventListener("change", async () => {
    if (!name.value.trim()) return;
    try { await api("PATCH", "/api/household", { name: name.value }); toast("Saved"); await loadState(); render(); } catch (x) { toast(x.message, true); }
  });
  const CURRENCIES = ["USD", "CAD", "EUR", "GBP", "AUD", "NZD", "MXN", "CHF", "SEK", "NOK", "DKK", "PLN", "JPY", "INR", "BRL", "ZAR", "SGD"];
  const currency = el("select.input", { disabled: !isOwner, "aria-label": "Currency", style: "max-width:220px" },
    [...new Set([S.household.currency, ...CURRENCIES])].map((c) => el("option", { value: c }, c)));
  currency.value = S.household.currency;
  currency.addEventListener("change", async () => {
    try { await api("PATCH", "/api/household", { currency: currency.value }); toast("Saved"); await loadState(); render(); } catch (x) { toast(x.message, true); }
  });
  const memberRow = (m) => el("tr", {},
    el("td", {}, m.display_name, m.id === S.user.id ? el("span.faint", {}, " (you)") : null, el("div.small.faint", {}, m.email || "")),
    el("td", {}, m.role === "owner" ? "Owner" : "Member"),
    el("td.r", {}, isOwner && m.id !== S.user.id ? el("div.actions", { style: "justify-content:flex-end;flex-wrap:nowrap" },
      el("button.btn.sm", { onclick: () => roleChange(m) }, m.role === "owner" ? "Make member" : "Make owner"),
      el("button.btn.sm.danger", { onclick: () => confirmBox(`Remove ${m.display_name}?`, "Their login stops working right away. Entries they added stay, still labeled with their name.", "Remove",
        async () => { await api("DELETE", `/api/household/members/${m.id}`); toast("Removed"); render(); }, true) }, "Remove")) : null));
  const roleChange = async (m) => {
    try { await api("PATCH", `/api/household/members/${m.id}`, { role: m.role === "owner" ? "member" : "owner" }); render(); } catch (x) { toast(x.message, true); }
  };

  body.append(
    el("div.card.pad.form", { style: "max-width:620px;margin-bottom:14px" },
      el("div.field", {}, el("label", {}, "Household name"), name,
        el("div.small.faint", {}, "Shown in the menu. Everyone in the household shares one budget.")),
      el("div.field", {}, el("label", {}, "Currency"), currency, el("div.small.faint", {}, "How amounts are shown. Changing it doesn't convert anything.")),
      isOwner ? el("div.actions", {}, el("a.btn.sm", { href: "/api/household/backup" }, "Download full backup (.db)"),
        el("span.small.faint", {}, "Everything in this budget as one file. Keep it somewhere safe.")) : null),
    el("div.group-h", {}, el("span", {}, "People")),
    el("div.card.table-wrap", { style: "max-width:620px" }, el("table.t", {}, el("tbody", {}, h.members.map(memberRow)))));

  if (isOwner) {
    const email = el("input.input", { type: "email", placeholder: "Their email (optional)", maxlength: 254, style: "flex:1;min-width:200px" });
    const out = el("div", {});
    body.append(
      el("div.group-h", {}, el("span", {}, "Invite someone")),
      el("form.card.pad.form", {
        style: "max-width:620px",
        onsubmit: async (e) => {
          e.preventDefault();
          try {
            const r = await api("POST", "/api/household/invites", { email: email.value });
            const box = el("input.input", { value: r.link, readonly: true, "aria-label": "Invite link" });
            out.replaceChildren(el("div.banner.info", { style: "margin:0;flex-direction:column;align-items:stretch" },
              el("div", {}, r.emailed ? `Sent to ${email.value}. You can also share this link yourself:` : "Send them this link. It works once, for 7 days:"),
              el("div.actions", { style: "flex-wrap:nowrap" }, box,
                el("button.btn.sm", { type: "button", onclick: async () => { box.select(); try { await navigator.clipboard.writeText(r.link); toast("Copied"); } catch { document.execCommand("copy"); toast("Copied"); } } }, "Copy"))));
            email.value = "";
            loadInvites();
          } catch (x) { toast(x.message, true); }
        },
      },
        el("p.small.muted", { style: "margin:0" }, "They'll make their own login and share this budget with you (for example, a partner)."),
        el("div.actions", {}, email, el("button.btn.primary", { type: "submit" }, "Create invite link")), out),
      el("div", { id: "invites" }));
    const loadInvites = async () => {
      const list = (await api("GET", "/api/household")).invites;
      const wrap = body.querySelector("#invites");
      wrap.replaceChildren(...(list.length ? [el("div.group-h", {}, el("span", {}, "Unused invites")),
        el("div.card.table-wrap", { style: "max-width:620px" }, el("table.t", {}, el("tbody", {}, list.map((i) => el("tr", {},
          el("td", {}, i.email || "Link only", el("div.small.faint", {}, `expires ${i.expires_at.slice(0, 10)}`)),
          el("td.r", {}, el("button.btn.sm", { onclick: async () => { await api("DELETE", `/api/household/invites/${i.id}`); loadInvites(); } }, "Cancel")))))))] : []));
    };
    loadInvites();

    const pw = el("input.input", { type: "password", autocomplete: "current-password", placeholder: "Your password", style: "flex:1;min-width:180px" });
    body.append(
      el("div.group-h", { style: "margin-top:28px" }, el("span", {}, "Danger zone")),
      el("form.card.pad.form.danger-card", {
        style: "max-width:620px",
        onsubmit: (e) => {
          e.preventDefault();
          confirmBox(`Delete ${h.name} for good?`, "This permanently deletes the whole budget (every month, entry, tracker and setting) and every login in the household. Download a CSV first if you might want it. This can't be undone.",
            "Delete everything", async () => {
              await api("POST", "/api/household/delete", { password: pw.value });
              S = null; CONFIG = null; location.hash = "#/login"; renderLogin(); toast("Household deleted");
            }, true);
        },
      },
        el("p.small.muted", { style: "margin:0" }, "Delete this household's budget and all of its logins."),
        el("div.actions", {}, pw, el("button.btn.danger", { type: "submit" }, "Delete household…"))));
  }
}

const THEMES = [["system", "Match device", "Light or dark, following your phone or computer"], ["light", "Light", "Clean and bright"],
  ["dark", "Dark", "Easy on the eyes at night"], ["noir", "Noir", "True black, great on OLED phones"], ["paper", "Paper", "Warm, low-glare light"]];
const ACCENTS = [["teal", "Teal", "#0f766e"], ["blue", "Blue", "#1d4ed8"], ["violet", "Violet", "#6d28d9"], ["rose", "Rose", "#be123c"],
  ["amber", "Amber", "#a14a08"], ["green", "Green", "#137336"], ["graphite", "Graphite", "#334155"]];

function settingsAppearance(body) {
  const save = async (patch) => {
    const theme = patch.theme || S.user.theme, accent = patch.accent || S.user.accent;
    applyTheme(theme, accent);  // instant; the server copy makes it follow you to other devices
    try { await api("PATCH", "/api/account", patch); await loadState(); draw(); } catch (x) { toast(x.message, true); }
  };
  const wrap = el("div", {});
  function draw() {
    wrap.replaceChildren(
      el("div.group-h", { style: "margin-top:0" }, el("span", {}, "Theme")),
      el("div.theme-grid", {}, THEMES.map(([k, label, sub]) => el("button.theme-card", {
        type: "button", class: S.user.theme === k ? "on" : null, "aria-pressed": S.user.theme === k, onclick: () => save({ theme: k }),
      },
        el("span.theme-preview", { "data-preview": k }, el("i"), el("b"), el("u")),
        el("span.theme-name", {}, label), el("span.small.faint", {}, sub)))),
      el("div.group-h", {}, el("span", {}, "Accent color")),
      el("div.accent-row", {}, ACCENTS.map(([k, label, hex]) => el("button.accent-dot", {
        type: "button", class: S.user.accent === k ? "on" : null, "aria-pressed": S.user.accent === k, "aria-label": label, title: label,
        style: `--dot:${hex}`, onclick: () => save({ accent: k }),
      }, el("span", {}, label)))),
      el("p.small.faint", {}, "Saved to your account, so it follows you to every device. Each person in the household picks their own."));
  }
  draw();
  body.append(wrap);
}

function settingsAccount(body) {
  const name = el("input.input", { value: S.user.display_name, maxlength: 60, "aria-label": "Your name" });
  name.addEventListener("change", async () => {
    if (!name.value.trim()) return;
    try { await api("PATCH", "/api/account", { display_name: name.value }); toast("Saved"); await loadState(); } catch (x) { toast(x.message, true); }
  });
  const email = el("input.input", { type: "email", value: S.user.email || "", maxlength: 254, placeholder: "you@example.com", "aria-label": "Email" });
  const saveEmail = el("button.btn.sm", { type: "button", onclick: async () => {
    try {
      const r = await api("PATCH", "/api/account", { email: email.value });
      toast(r.verify_sent ? `Check ${email.value} for a link to confirm it` : "No change");
    } catch (x) { toast(x.message, true); }
  } }, "Save email");
  const cur = el("input.input", { type: "password", autocomplete: "current-password", "aria-label": "Current password" });
  const nw = el("input.input", { type: "password", autocomplete: "new-password", minlength: 10, "aria-label": "New password" });
  body.append(
    el("div.card.pad.form", { style: "max-width:520px;margin-bottom:14px" },
      el("div.field", {}, el("label", {}, "Your name"), name, el("div.small.faint", {}, "Shown on entries you add.")),
      el("div.field", {}, el("label", {}, "Email"), el("div.actions", { style: "flex-wrap:nowrap" }, email, saveEmail),
        el("div.small.faint", {}, S.user.email ? "Used to log in and to reset your password." : "Add one so you can reset your password if you forget it. We'll send a link to confirm it.")),
      S.user.username !== S.user.email ? el("div.small.faint", {}, `Username: ${S.user.username}`) : null),
    el("form.card.pad.form", {
      style: "max-width:520px;margin-bottom:14px",
      onsubmit: async (e) => {
        e.preventDefault();
        try { await api("POST", "/api/account/password", { current: cur.value, new: nw.value }); cur.value = nw.value = ""; toast("Password changed. Other devices were signed out."); }
        catch (x) { toast(x.message, true); }
      },
    },
      el("h2.card-title", {}, "Change password"),
      el("div.row2", {}, el("div.field", {}, el("label", {}, "Current"), cur), el("div.field", {}, el("label", {}, "New (10+ characters)"), nw)),
      el("div.actions", {}, el("button.btn.primary", { type: "submit" }, "Change password"))),
    el("div.card.pad.form", { style: "max-width:520px" },
      el("div.actions", {},
        el("a.btn", { href: "/api/export.csv" }, "Download all entries (CSV)"),
        el("a.btn", { href: "#/privacy" }, "Privacy"),
        el("button.btn", { onclick: async () => { try { const r = await api("POST", "/api/account/logout-others"); toast(r.signed_out ? `Signed out ${r.signed_out} other device${r.signed_out > 1 ? "s" : ""}` : "No other devices were signed in"); } catch (x) { toast(x.message, true); } } }, "Sign out other devices"),
        el("button.btn.danger", { onclick: async () => { await api("POST", "/api/logout"); S = null; location.hash = "#/login"; } }, "Log out")),
      el("p.small.faint.about", { style: "margin:0" })));
  getConfig().then(() => {
    const about = body.querySelector("p.about");
    if (!about) return;
    about.append(`Budget ${CONFIG.version || ""} · free software under the `,
      el("a", { href: "https://www.gnu.org/licenses/agpl-3.0.html", target: "_blank", rel: "noopener noreferrer" }, "AGPL-3.0"),
      CONFIG.source ? [" · ", el("a", { href: CONFIG.source, target: "_blank", rel: "noopener noreferrer" }, "Source code")] : "");
  });
}

// ------------------------------------------------------------------ bank CSV import
// Everything up to "Add" happens in the browser: the file is read and matched here and only the
// rows you tick are sent to the server.

function parseCSV(text) {
  const rows = []; let row = [], cell = "", q = false;
  text = text.replace(/^﻿/, "");
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') q = false;
      else cell += ch;
    } else if (ch === '"') q = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(cell); cell = "";
      if (row.some((c) => c.trim() !== "")) rows.push(row);
      row = [];
    } else cell += ch;
  }
  row.push(cell);
  if (row.some((c) => c.trim() !== "")) rows.push(row);
  return rows;
}

function parseDateLoose(s) {
  s = (s || "").trim();
  let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return `${m[1]}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}`;
  m = s.match(/^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$/);
  if (m) { const y = m[3].length === 2 ? "20" + m[3] : m[3]; return `${y}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}`; }
  return null;
}

function moneyLoose(s) {
  s = (s || "").trim();
  if (!s) return null;
  const neg = /^\(.*\)$/.test(s) || s.includes("-");
  const n = parseFloat(s.replace(/[^0-9.]/g, ""));
  return isNaN(n) ? null : Math.round(n * 100) * (neg ? -1 : 1);
}

async function renderImport(body) {
  const file = el("input", { type: "file", accept: ".csv,text/csv", "aria-label": "Bank CSV file" });
  const area = el("div", {});
  body.append(
    el("p.muted", { style: "margin-top:0;max-width:680px" },
      "Download recent transactions from your bank's website as a CSV file (US Bank, Capital One and most others offer this), then pick it here. ",
      "You'll review every row before anything is added, and rows that look already logged are left unticked."),
    el("div.card.pad", { style: "max-width:680px;margin-bottom:14px" }, file),
    area);
  file.addEventListener("change", async () => {
    const f = file.files[0];
    if (!f) return;
    if (f.size > 2_000_000) return toast("That file is too big (2 MB max)", true);
    const rows = parseCSV(await f.text());
    if (rows.length < 2) return toast("That file doesn't look like a CSV with transactions", true);
    importMapping(area, rows);
  });
}

function importMapping(area, rows) {
  const head = rows[0].map((h) => h.trim());
  const find = (...res) => { for (const re of res) { const i = head.findIndex((h) => re.test(h)); if (i >= 0) return i; } return -1; };
  const sel = (idx, allowNone) => {
    const s = el("select.input", {}, allowNone ? el("option", { value: "-1" }, "—") : null, head.map((h, i) => el("option", { value: i }, h || `Column ${i + 1}`)));
    s.value = String(idx); return s;
  };
  const dateCol = sel(Math.max(0, find(/^transaction date$/i, /^date$/i, /date/i)));
  const descCol = sel(Math.max(0, find(/^description$/i, /^name$/i, /payee|merchant|description|transaction/i, /memo/i)));
  const debitIdx = find(/^debit$/i, /withdraw/i), creditIdx = find(/^credit$/i, /deposit/i), amtIdx = find(/^amount$/i, /amount/i);
  const mode = el("select.input", {},
    el("option", { value: "neg" }, "One amount column: negative = spending (US Bank)"),
    el("option", { value: "pos" }, "One amount column: positive = spending"),
    el("option", { value: "split" }, "Separate debit and credit columns (Capital One)"));
  mode.value = debitIdx >= 0 && creditIdx >= 0 ? "split" : "neg";
  const amtCol = sel(Math.max(0, amtIdx)), debitCol = sel(debitIdx, true), creditCol = sel(creditIdx, true);
  const amtField = el("div.field", {}, el("label", {}, "Amount column"), amtCol);
  const splitFields = el("div.row2", {}, el("div.field", {}, el("label", {}, "Debit (spending)"), debitCol), el("div.field", {}, el("label", {}, "Credit (money in)"), creditCol));
  const showMode = () => { amtField.classList.toggle("hidden", mode.value === "split"); splitFields.classList.toggle("hidden", mode.value !== "split"); };
  mode.addEventListener("change", showMode); showMode();

  area.replaceChildren(el("div.card.pad.form", { style: "max-width:680px;margin-bottom:14px" },
    el("h2.card-title", {}, `Found ${rows.length - 1} rows. Which columns are which?`),
    el("div.row2", {}, el("div.field", {}, el("label", {}, "Date"), dateCol), el("div.field", {}, el("label", {}, "Description"), descCol)),
    el("div.field", {}, el("label", {}, "Amounts"), mode), amtField, splitFields,
    el("div.actions", {}, el("button.btn.primary", {
      onclick: async () => {
        const out = [];
        for (const r of rows.slice(1)) {
          const date = parseDateLoose(r[+dateCol.value]);
          let cents;
          if (mode.value === "split") {
            const d = moneyLoose(r[+debitCol.value]), c = moneyLoose(r[+creditCol.value]);
            cents = d ? Math.abs(d) : c ? -Math.abs(c) : null;
          } else {
            const a = moneyLoose(r[+amtCol.value]);
            cents = a === null ? null : mode.value === "neg" ? -a : a;
          }
          if (!date || !cents) continue;
          out.push({ date, description: (r[+descCol.value] || "").trim().slice(0, 200), amount_cents: cents });
        }
        if (!out.length) return toast("No rows with a date and amount. Check the columns.", true);
        try { await importReview(area, out); } catch (x) { toast(x.message, true); }
      },
    }, "Next: review rows"))));
}

async function importReview(area, rows) {
  rows.sort((a, b) => b.date.localeCompare(a.date));
  const [checks, sugg] = await Promise.all([api("POST", "/api/import/check", { rows }), getSuggestions()]);
  const opens = openMonths();
  const cats = S.categories.filter((c) => !c.archived);
  // Guess a category: an earlier entry whose description appears in the bank's text (e.g. "costco" in "COSTCO WHSE #0123").
  const guesses = sugg.filter((s) => s.description.length >= 3).map((s) => ({ ...s, k: s.description.toLowerCase() }));
  const guess = (desc) => {
    const d = desc.toLowerCase();
    const hit = guesses.find((s) => s.k === d) || guesses.find((s) => d.includes(s.k));
    return hit ? hit.category_id : "";
  };
  // Card payments and transfers move money you've already budgeted; adding them would double-count.
  const PAYMENT = /\b(autopay|payment|pymt|thank you|transfer|xfer|online pmt|epay)\b/i;
  const items = rows.map((r, i) => {
    const dup = checks[i].duplicate_of;
    const payment = PAYMENT.test(r.description);
    const ym = opens.includes(r.date.slice(0, 7)) ? r.date.slice(0, 7) : latestOpen();
    const on = el("input", { type: "checkbox", checked: !dup && !payment, "aria-label": "Add this row" });
    const desc = el("input.input", { value: r.description, maxlength: 200, "aria-label": "Description" });
    const cat = el("select.input", { "aria-label": "Category" }, el("option", { value: "" }, "Pick…"), cats.map((c) => el("option", { value: c.id }, c.name)));
    cat.value = String(guess(r.description));
    const month = el("select.input", { "aria-label": "Counts toward" }, opens.map((m) => el("option", { value: m }, ymLabel(m, true))));
    month.value = ym;
    const row = el("div.imp-row", { class: dup || payment ? "dup" : null }, on,
      el("div.imp-main", {}, desc,
        el("div.small.faint", {}, dateLabel(r.date),
          dup ? el("span.warn-text", {}, ` · looks already logged: “${dup.description || "(no description)"}”${dup.category ? " in " + dup.category : ""}`) : null,
          payment && !dup ? el("span.warn-text", {}, " · looks like a card payment or transfer") : null,
          ym !== r.date.slice(0, 7) ? el("span", {}, ` · ${ymLabel(r.date.slice(0, 7), true)} is closed`) : null)),
      el("div.imp-amt.num", { class: r.amount_cents < 0 ? "pos" : null }, (r.amount_cents < 0 ? "+" : "") + money(Math.abs(r.amount_cents))),
      cat, month);
    return { r, on, desc, cat, month, row };
  });
  const count = el("strong", {});
  const upd = () => { count.textContent = String(items.filter((x) => x.on.checked).length); };
  area.replaceChildren(
    el("div.section-h", {}, el("h2", {}, "Review"), el("span.muted", {}, count, " rows will be added")),
    el("p.small.faint", { style: "margin-top:0" }, "Amounts with + are money coming in (refunds, deposits). Untick anything you don't want, like credit card payments or transfers."),
    el("div.card.imp-list", { onchange: upd }, items.map((x) => x.row)),
    el("div.actions", { style: "margin:14px 0 30px" }, el("button.btn.primary", {
      onclick: async (e) => {
        const chosen = items.filter((x) => x.on.checked);
        const missing = chosen.filter((x) => !x.cat.value);
        if (missing.length) { missing[0].cat.focus(); return toast(`Pick a category for ${missing.length} row${missing.length > 1 ? "s" : ""}`, true); }
        e.target.disabled = true;
        try {
          const r = await api("POST", "/api/import/commit", { rows: chosen.map((x) => ({
            date: x.r.date, description: x.desc.value, amount_cents: x.r.amount_cents, category_id: +x.cat.value, ym: x.month.value })) });
          suggestionCache = null;
          toast(`Added ${r.added} entries`);
          go(`#/month/${latestOpen()}`);
        } catch (x) { toast(x.message, true); e.target.disabled = false; }
      },
    }, "Add ticked rows")));
  upd();
}

// ------------------------------------------------------------------ router

async function render() {
  const raw = location.hash.replace(/^#/, "") || "/dash";
  const [path, query = ""] = raw.split("?");
  const params = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean);
  try {
    switch (parts[0]) {  // screens that work logged out
      case "login": return S ? go("#/dash") : await renderLogin();
      case "signup": return await renderSignup();
      case "verify": return await renderVerify(params);
      case "forgot": return renderForgot();
      case "reset": return renderReset(params);
      case "join": return await renderJoin(params);
      case "privacy": return renderPrivacy();
    }
    // A brand-new household starts with the setup screen.
    if (!S || !S.household.setup_done) {
      await loadState();
      if (!S.household.setup_done) return await renderSetup();
    }
    switch (parts[0]) {
      case "dash": return await renderDashboard(parts[1]);
      case "month": return await renderMonth(parts[1]);
      case "cat": return await renderCategory(+parts[1], parts[2]);
      case "add": return await renderEntryForm(params);
      case "batch": return await renderBatch(params);
      case "edit": return await renderEntryForm(params, +parts[1]);
      case "search": return await renderSearch(decodeURIComponent(parts.slice(1).join("/")));
      case "reports": return await renderReports(params);
      case "trackers": return await renderTrackers(params);
      case "tracker": return await renderTracker(+parts[1], params);
      case "settings": return await renderSettings(params);
      default: return go("#/dash");
    }
  } catch (x) {
    if (x.message !== "Please log in") toast(x.message, true);
  }
}

window.addEventListener("hashchange", render);
render();

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
