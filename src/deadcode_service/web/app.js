// Dashboard web de deadcode-service.
//
// Solo llama a la API que ya existe:
//   POST /analyses         -> crear un análisis
//   GET  /analyses         -> la lista (liviana: solo el resumen)
//   GET  /analyses/{id}    -> un análisis completo, con sus hallazgos
//
// ⚠️ SEGURIDAD (XSS): los nombres de funciones y archivos vienen de repos
// AJENOS. Un repo podría tener un archivo llamado "<img src=x onerror=...>".
// Por eso TODO texto se inserta con textContent (el navegador lo muestra
// tal cual, nunca lo interpreta como HTML). En este archivo NO se usa
// innerHTML. Un test (tests/test_web.py) lo comprueba.

"use strict";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PRIORITIES = ["high", "medium", "low"];
const PRIORITY_NAMES = { high: "alta", medium: "media", low: "baja" };
const STATUS_NAMES = { queued: "en cola", running: "analizando", finished: "terminado", failed: "falló" };
const MAX_ROWS = 300; // filas de hallazgos que se dibujan a la vez

// Nivel de los análisis creados desde ESTA página: la tabla no guarda el
// nivel, y mientras no terminan el resultado aún no dice si es nivel 2.
const createdLevels = new Map();

let refreshTimer = null;

// --- Ayudantes ---------------------------------------------------------------

// Crea un elemento. "children" puede ser texto (se pone como TEXTO, seguro)
// u otros elementos.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) { // listas dentro de listas, a cualquier profundidad
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function $(id) {
  return document.getElementById(id);
}

async function api(path, options) {
  const response = await fetch(path, options);
  let body = null;
  try {
    body = await response.json();
  } catch {
    // respuesta sin JSON (ej. un 502 del proxy): body queda en null
  }
  return { ok: response.ok, status: response.status, body };
}

// Mensaje legible de un error de FastAPI (422 trae una lista; 503 un texto).
function errorText(body, status) {
  const detail = body && body.detail;
  if (Array.isArray(detail) && detail.length) return detail[0].msg;
  if (typeof detail === "string") return detail;
  return `Error HTTP ${status}`;
}

function shortRepo(url) {
  return url.replace(/^https:\/\/github\.com\//, "");
}

function formatDate(iso) {
  return iso ? new Date(iso).toLocaleString("es") : "";
}

function formatDuration(start, end) {
  if (!start || !end) return "";
  const seconds = Math.round((new Date(end) - new Date(start)) / 1000);
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}

function statusBadge(status) {
  return el("span", { class: `badge st-${status}` }, STATUS_NAMES[status] || status);
}

// Enlace a la línea exacta en GitHub, en el commit analizado.
// Solo se arma si todo tiene la forma esperada (repo_url ya la validó la
// API; el commit debe ser un hash; cada parte de la ruta va codificada).
function githubLink(repoUrl, commit, file, line) {
  if (!/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+$/.test(repoUrl)) return null;
  if (!/^[0-9a-f]{40}$/.test(commit || "")) return null;
  const path = file.split("/").map(encodeURIComponent).join("/");
  return `${repoUrl}/blob/${commit}/${path}#L${Number(line)}`;
}

// --- Nuevo análisis --------------------------------------------------------------

// ¿Este entorno puede correr el nivel 2? (en Compose no: ver GET /config).
// Si no, la casilla se desactiva y se explica por qué ANTES de intentarlo.
async function loadConfig() {
  const { ok, body } = await api("/config").catch(() => ({ ok: false }));
  if (!ok || body.level2_available) return; // si no responde, la API avisará al enviar
  const box = $("dynamic");
  box.checked = false;
  box.disabled = true;
  $("dynamic-label").classList.add("disabled");
  $("dynamic-note").hidden = false;
}

async function submitAnalysis(event) {
  event.preventDefault();
  const button = event.target.querySelector("button");
  const message = $("form-message");
  const dynamic = $("dynamic").checked;
  button.disabled = true;
  message.hidden = true;

  const { ok, status, body } = await api("/analyses", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_url: $("repo-url").value.trim(), dynamic }),
  }).catch(() => ({ ok: false, status: 0, body: null }));

  button.disabled = false;
  message.hidden = false;
  if (ok) {
    createdLevels.set(body.id, dynamic ? 2 : 1);
    message.className = "message ok";
    message.textContent = dynamic
      ? "Encolado (nivel 2). Puede tardar varios minutos: la lista se actualiza sola."
      : "Encolado. Suele tardar menos de un minuto: la lista se actualiza sola.";
    $("repo-url").value = "";
    location.hash = ""; // volver a la lista
    loadList();
  } else {
    message.className = "message error";
    message.textContent = status === 0 ? "La API no responde" : errorText(body, status);
  }
}

// --- Lista -------------------------------------------------------------------------

function levelOf(item) {
  if (item.dynamic_status) return 2; // el resultado trae la sección del nivel 2
  if (item.status === "finished" || item.status === "failed") return 1;
  return createdLevels.get(item.id) || null; // aún no se sabe
}

async function loadList() {
  clearTimeout(refreshTimer);
  const { ok, status, body } = await api("/analyses").catch(() => ({ ok: false, status: 0 }));
  const tbody = $("analyses");
  if (!ok) {
    $("list-status").textContent = status === 0 ? "La API no responde" : `Error HTTP ${status}`;
    refreshTimer = setTimeout(loadList, 10000);
    return;
  }

  tbody.replaceChildren(
    ...body.map((item) => {
      const counts = item.summary && item.summary.by_priority;
      const level = levelOf(item);
      return el(
        "tr",
        { class: "clickable", onclick: () => (location.hash = item.id), title: "Ver el detalle" },
        el("td", {}, shortRepo(item.repo_url)),
        el("td", {}, level ? String(level) : "–"),
        el("td", {}, statusBadge(item.status)),
        el(
          "td",
          {},
          counts
            ? PRIORITIES.map((p, i) => [i ? " / " : "", el("span", { class: `p-${p}` }, String(counts[p] || 0))])
            : el("span", { class: "muted small" }, item.error || "")
        ),
        el("td", { class: "small" }, formatDate(item.created_at)),
        el("td", { class: "small" }, formatDuration(item.created_at, item.finished_at))
      );
    })
  );
  if (!body.length) {
    tbody.append(el("tr", {}, el("td", { colspan: "6", class: "muted" }, "Todavía no hay análisis.")));
  }

  // Mientras alguno esté en cola o corriendo, revisar cada 5 s.
  const pending = body.some((item) => item.status === "queued" || item.status === "running");
  $("list-status").textContent = pending ? "Actualizando cada 5 s…" : "";
  if (pending) refreshTimer = setTimeout(loadList, 5000);
}

// --- Detalle -------------------------------------------------------------------------

function stat(value, label, cls) {
  return el("div", { class: "stat" }, el("div", { class: `value ${cls || ""}` }, value), el("div", { class: "label" }, label));
}

function dynamicSection(result) {
  const dynamic = result.dynamic;
  const card = el("section", { class: "card" }, el("h2", {}, "Nivel 2: tests del repo con coverage.py"));
  if (!dynamic) {
    card.append(el("p", { class: "muted" }, "Este análisis fue solo de nivel 1 (Vulture). Marca \"Con tests\" al crear uno para confirmar o descartar cada hallazgo."));
    return card;
  }
  if (dynamic.status !== "finished") {
    card.append(
      el("p", {}, el("span", { class: "badge st-failed" }, "no se pudo"), " ", dynamic.error || ""),
      el("p", { class: "muted small" }, "Los hallazgos de abajo son los de Vulture (nivel 1), sin confirmar.")
    );
    return card;
  }
  const cross = dynamic.cross;
  card.append(
    el(
      "div",
      { class: "stats" },
      stat(`${dynamic.coverage_percent}%`, "cobertura de los tests"),
      stat(String(cross.confirmed), "confirmados (nunca se ejecutaron)", "p-high"),
      stat(String(cross.false_positives), "falsos positivos de Vulture (sí se ejecutaron)", "p-low"),
      stat(String(cross.not_applicable), "no aplica (imports, variables…)")
    ),
    el("p", { class: "muted small" }, "Tests: ", dynamic.tests.summary || "", dynamic.tests.timed_out ? " (cortados por tiempo)" : "")
  );
  return card;
}

function executedCell(finding) {
  if (finding.executed === true) return el("span", { class: "p-low", title: "Los tests lo ejecutaron" }, "sí se ejecutó");
  if (finding.executed === false) return el("span", { class: "p-high", title: "Los tests nunca lo ejecutaron" }, "nunca");
  return el("span", { class: "muted" }, "–");
}

function findingsSection(analysis) {
  const result = analysis.result;
  const findings = result.dead_code || [];
  const hasDynamic = findings.some((f) => "executed" in f);
  const state = { priority: "high", codeOnly: true };

  const tbody = el("tbody");
  const counter = el("span", { class: "muted small" });
  const buttons = {};

  function draw() {
    const visible = findings.filter(
      (f) => (state.priority === "all" || f.priority === state.priority) && (!state.codeOnly || f.location === "code")
    );
    for (const [key, button] of Object.entries(buttons)) button.classList.toggle("active", key === state.priority);
    counter.textContent =
      visible.length > MAX_ROWS ? `mostrando ${MAX_ROWS} de ${visible.length}` : `${visible.length} hallazgos`;
    tbody.replaceChildren(
      ...visible.slice(0, MAX_ROWS).map((f) => {
        const href = githubLink(analysis.repo_url, result.commit, f.file, f.line);
        const where = `${f.file}:${f.line}`;
        const changed = f.static_priority && f.static_priority !== f.priority;
        return el(
          "tr",
          {},
          el(
            "td",
            {},
            el("span", { class: `badge p-${f.priority}` }, PRIORITY_NAMES[f.priority] || f.priority),
            changed ? el("div", { class: "muted small" }, `Vulture: ${PRIORITY_NAMES[f.static_priority]}`) : null
          ),
          el("td", { class: "mono" }, f.name),
          el("td", { class: "small" }, f.type),
          el(
            "td",
            { class: "mono" },
            href ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, where) : where
          ),
          hasDynamic ? el("td", { class: "small" }, executedCell(f)) : null,
          el("td", { class: "reason" }, f.reason)
        );
      })
    );
    if (!visible.length) {
      tbody.append(el("tr", {}, el("td", { colspan: "6", class: "muted" }, "Nada con este filtro.")));
    }
  }

  const counts = result.summary ? result.summary.by_priority : {};
  for (const key of [...PRIORITIES, "all"]) {
    const label = key === "all" ? "todas" : `${PRIORITY_NAMES[key]} (${counts[key] || 0})`;
    buttons[key] = el("button", { type: "button", onclick: () => ((state.priority = key), draw()) }, label);
  }
  const codeOnly = el("input", { type: "checkbox", checked: "checked" });
  codeOnly.addEventListener("change", () => ((state.codeOnly = codeOnly.checked), draw()));

  draw();
  return el(
    "section",
    { class: "card" },
    el("div", { class: "row-between" }, el("h2", {}, "Hallazgos"), counter),
    el(
      "div",
      { class: "filters" },
      "Prioridad:",
      Object.values(buttons),
      el("label", { class: "check" }, codeOnly, "solo código (sin tests, docs ni ejemplos)")
    ),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            el("th", {}, "Prioridad"),
            el("th", {}, "Nombre"),
            el("th", {}, "Tipo"),
            el("th", {}, "Dónde"),
            hasDynamic ? el("th", {}, "¿Lo ejecutaron los tests?") : null,
            el("th", {}, "Por qué")
          )
        ),
        tbody
      )
    ),
    el("p", { class: "muted small" }, result.note || "")
  );
}

async function loadDetail(id) {
  clearTimeout(refreshTimer);
  const box = $("detail");
  box.replaceChildren(el("p", { class: "muted" }, "Cargando…"));
  const { ok, status, body } = await api(`/analyses/${id}`).catch(() => ({ ok: false, status: 0 }));
  if (location.hash.slice(1) !== id) return; // el usuario ya se fue a otra parte
  if (!ok) {
    box.replaceChildren(el("p", { class: "message error" }, status === 404 ? "Análisis no encontrado" : errorText(body, status)));
    return;
  }

  const head = el(
    "section",
    { class: "card" },
    el(
      "div",
      { class: "row-between" },
      el("h2", {}, shortRepo(body.repo_url)),
      statusBadge(body.status)
    )
  );
  const info = el("dl", { class: "dl" });
  const add = (label, value) => value !== null && value !== undefined && value !== "" && info.append(el("dt", {}, label), el("dd", {}, value));
  add("Repo", el("a", { href: body.repo_url, target: "_blank", rel: "noopener noreferrer" }, body.repo_url));
  add("Creado", formatDate(body.created_at));
  add("Duración", formatDuration(body.created_at, body.finished_at));
  const parts = [head];

  if (body.status === "queued" || body.status === "running") {
    head.append(info, el("p", { class: "muted" }, "Todavía en curso: esta página se actualiza cada 5 s."));
    refreshTimer = setTimeout(() => loadDetail(id), 5000);
  } else if (body.status === "failed") {
    head.append(info, el("p", { class: "message error" }, body.error || "Falló sin mensaje"));
  } else {
    const result = body.result;
    add("Commit", result.commit ? el("span", { class: "mono" }, result.commit.slice(0, 12)) : null);
    if (result.files_analyzed !== undefined) {
      add("Archivos", `${result.files_analyzed} archivos .py, ${result.lines_analyzed} líneas`);
    }
    add("Frameworks", (result.frameworks_detected || []).join(", ") || "ninguno detectado");
    // Los análisis muy viejos (antes de la clasificación) no traen resumen.
    const summary = result.summary || {};
    const counts = summary.by_priority || {};
    head.append(
      info,
      el(
        "div",
        { class: "stats" },
        stat(String(counts.high ?? "–"), "prioridad alta", "p-high"),
        stat(String(counts.medium ?? "–"), "prioridad media", "p-medium"),
        stat(String(counts.low ?? "–"), "prioridad baja", "p-low"),
        stat(String((summary.by_location || {}).code ?? "–"), "en el código (no tests/docs)")
      )
    );
    parts.push(dynamicSection(result), findingsSection(body));
  }
  box.replaceChildren(...parts);
}

// --- Navegación: "#" = lista, "#<id>" = detalle -----------------------------------------

function route() {
  const id = location.hash.slice(1);
  const showDetail = UUID.test(id);
  $("list-view").hidden = showDetail;
  $("detail-view").hidden = !showDetail;
  if (showDetail) {
    loadDetail(id);
  } else {
    loadList();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("new-analysis").addEventListener("submit", submitAnalysis);
  for (const link of document.querySelectorAll(".back, #home")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      history.pushState(null, "", location.pathname); // quita el "#id"
      route();
    });
  }
  window.addEventListener("hashchange", route);
  loadConfig();
  route();
});
