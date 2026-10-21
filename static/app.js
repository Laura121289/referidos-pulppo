// Referidos · Pulppo — vista del asesor y de admins (Finanzas / KAM).
const $ = (s, r = document) => r.querySelector(s);
const CONFIG = window.CONFIG || {};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mxn = (v) => (v || 0).toLocaleString("es-MX", { style: "currency", currency: "MXN", minimumFractionDigits: 2 });
const mxn0 = (v) => (v || 0).toLocaleString("es-MX", { style: "currency", currency: "MXN", maximumFractionDigits: 0 });
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
const mesTxt = (m) => (m ? `${MESES[+m.slice(5, 7) - 1]} ${m.slice(0, 4)}` : "");
const fecha = (iso) => { if (!iso) return ""; const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso); return d.toLocaleDateString("es-MX", { day: "numeric", month: "short", year: "numeric" }); };
const pct = (v) => `${Math.round(v * 100)} %`;
const NIVEL = { estandar: "Estándar", profesional: "Profesional", elite: "Élite" };
const ESTADO = {
  disponible: "Por facturar", proxima_ventana: "Próxima ventana", facturado: "Facturado", aprobado: "Aprobado", rechazado: "Rechazado",
  pagado: "Pagado", en_curso: "Mes en curso", pendiente_regalia: "Regalía pendiente", perdido: "No cobrable", vencido: "Vencido", excluido: "No aplica",
};
const chip = (e) => `<span class="chip ${esc(e)}">${esc(ESTADO[e] || e)}</span>`;

let YO = null, TAB = null, SOLS = [], FILTRO = "facturado", SEL = null;

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401) { mostrarLogin(); throw new Error("sin sesión"); }
  const d = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw new Error(d?.error || `Error ${r.status}`);
  return d;
}
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

// ---------------------------------------------------------------- sesión

async function iniciar() {
  try { YO = await api("/api/yo"); } catch { return; }
  $("#login").hidden = true; $("#app").hidden = false;
  $("#quien").textContent = YO.nombre; $("#sesion").hidden = false;
  const tabs = [];
  if (YO.agente_id) tabs.push(["mio", "Mis beneficios"]);
  if (YO.admin) tabs.push(["bandeja", "Facturas"], ["beneficios", "Beneficios"], ["referidores", "Referidores"], ["simulacion", "Simulación"], ["excepciones", "Excepciones"]);
  if (tabs.length > 1) {
    $("#tabs").innerHTML = tabs.map(([k, l]) => `<button type="button" data-tab="${k}">${l}</button>`).join("");
    $("#tabs").hidden = false;
  }
  let guardado = null;
  try { guardado = sessionStorage.getItem("tab"); } catch { /* sin almacenamiento */ }
  const pedida = new URLSearchParams(location.search).get("tab");  // ligas directas: /?tab=beneficios
  ir([pedida, guardado].find((t) => tabs.some(([k]) => k === t)) || (YO.admin ? "bandeja" : "mio"));
}

function ir(tab) {
  TAB = tab;
  try { sessionStorage.setItem("tab", tab); } catch { /* sin almacenamiento */ }
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
  ({ mio: () => vistaAsesor(), bandeja: vistaBandeja, beneficios: vistaBeneficios, referidores: vistaReferidores, simulacion: vistaSimulacion, excepciones: vistaExcepciones })[tab]();
}

function mostrarLogin() {
  $("#app").hidden = true; $("#login").hidden = false; $("#tabs").hidden = true; $("#sesion").hidden = true;
  if (new URLSearchParams(location.search).get("error") === "enlace") error("El enlace venció o no es válido. Pide uno nuevo.");
  if (CONFIG.local) $("#form-local").hidden = false;
  if (!CONFIG.googleClientId) { $("#sin-google").hidden = false; return; }
  const s = Object.assign(document.createElement("script"), { src: "https://accounts.google.com/gsi/client", async: true });
  s.onload = () => {
    google.accounts.id.initialize({ client_id: CONFIG.googleClientId, callback: entrarGoogle, ux_mode: "popup" });
    google.accounts.id.renderButton($("#google-btn"), { theme: "outline", size: "large", text: "continue_with", shape: "rectangular", locale: "es" });
  };
  document.head.appendChild(s);
}

async function entrarGoogle({ credential }) {
  const r = await fetch("/auth/google", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ credential }) });
  if (r.ok) return iniciar();
  const d = await r.json().catch(() => ({}));
  error(d.error || "No se pudo iniciar sesión. Intenta de nuevo.");
}
function error(msg) { $("#login-error").textContent = msg; $("#login-error").hidden = false; }

$("#form-enlace").addEventListener("submit", async (e) => {
  e.preventDefault();
  await fetch("/auth/enlace", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: $("#correo").value }) });
  $("#enlace-ok").hidden = false;
});
$("#form-local").addEventListener("submit", (e) => { e.preventDefault(); location.href = `/auth/local?email=${encodeURIComponent($("#correo-local").value)}`; });
$("#salir").addEventListener("click", async () => { await fetch("/auth/salir", { method: "POST" }); location.href = "/"; });
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) ir(b.dataset.tab); });

// ---------------------------------------------------------------- vista del asesor

async function vistaAsesor(como = null, nombreComo = "") {
  $("#app").innerHTML = `<p class="muted">Calculando tus beneficios…</p>`;
  let R;
  try { R = await api(`/api/resumen${como ? `?como=${encodeURIComponent(como)}` : ""}`); }
  catch (e) { $("#app").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  const a = R.agente, t = R.totales, ro = R.solo_lectura;
  const nivelTxt = a.excepcion ? `${NIVEL[a.nivel]} · excepción Master Broker` : NIVEL[a.nivel];
  const red = [1, 2, 3].map((n) => `${R.red[n] || 0} en nivel ${n}`).join(" · ");
  $("#app").innerHTML = `
    ${ro ? `<div class="banner-como"><span>Viendo como <b>${esc(a.nombre)}</b> (solo lectura)</span><button class="link" id="volver-ref">Volver a referidores</button></div>` : ""}
    <header class="encabezado">
      <p class="eyebrow">Programa de referidos</p>
      <h1>${esc(a.nombre)}</h1>
      <div class="meta">
        <span>${esc(a.inmobiliaria)}${a.tipo === "master" ? " · Master Broker" : ""}</span>
        <span>Pulppo Plus: <b>${esc(nivelTxt)}</b></span>
        <span>Comisión de los últimos 6 meses: <b>${mxn0(a.comision_6m)}</b></span>
        <span>Red: <b>${red}</b></span>
      </div>
      <div class="acento"></div>
    </header>
    ${!a.afiliado ? `<div class="caja alerta" style="margin-bottom:24px"><b>Completa el Módulo 1 de Academia.</b> Sin él no cobras beneficios por tus referidos.</div>` : ""}
    ${avisosHTML(R.avisos)}
    <section class="bloque">
      <div class="stats">
        ${stat(t.a_cobrar, "A cobrar", "Listo para facturar")}
        ${stat(t.en_proceso, "En proceso", "Facturado o aprobado")}
        ${stat(t.cobrado, "Cobrado", "Pagado por Pulppo")}
        ${stat(t.a_futuro, "A futuro", "Mes en curso o regalía pendiente")}
      </div>
      <p class="nota">Programa relanzado: cuenta desde ${mesTxt(R.inicio_programa)}. Lo anterior está en “Programa anterior”, más abajo.</p>
    </section>
    <section class="bloque">${porFacturarHTML(R, ro)}</section>
    ${R.solicitudes.length ? `<section class="bloque"><header><h2>Mis facturas</h2></header>${tablaSolicitudes(R.solicitudes)}</section>` : ""}
    <section class="bloque" id="mis-referidos"><header><h2>${ro ? "Sus referidos" : "Mis referidos"}</h2><span class="nota">Referidos que te pueden generar beneficio, por nivel.</span></header>${referidosHTML(R.referidos)}</section>
    <section class="bloque"><header><h2>Detalle por mes</h2><span class="nota">Beneficio que te generó cada referido en el mes.</span></header>${detalleMeses(R.lineas)}</section>
    <section class="bloque">${anteriorHTML(R.anterior, ro)}</section>
    <section class="bloque"><header><h2>Cómo se calcula</h2></header>${reglasHTML(R.reglas)}</section>`;
  $("#volver-ref")?.addEventListener("click", () => ir("referidores"));
  REFERIDOS = R.referidos;
  $("#mis-referidos").addEventListener("click", (e) => { const b = e.target.closest("[data-nivel]"); if (b) { NIVEL_REF = b.dataset.nivel; pintarReferidos(); } });
  $("#avisos-ok")?.addEventListener("click", async () => { await post("/api/avisos/leidos"); $(".avisos").remove(); });
  document.querySelectorAll("form[data-facturar]").forEach((f) => f.addEventListener("submit", subirFactura));
}

let REFERIDOS = [], NIVEL_REF = "";
function referidosHTML(xs) {
  NIVEL_REF = "";
  if (!xs.length) return `<p class="muted">Aún no tienes referidos que te generen beneficio.</p>`;
  return `<div class="filtros">${[["", "Todos"], ["1", "Nivel 1"], ["2", "Nivel 2"], ["3", "Nivel 3"]].map(([k, l]) =>
      `<button class="filtro" data-nivel="${k}" aria-pressed="${k === ""}">${l} (${k ? xs.filter((x) => String(x.nivel) === k).length : xs.length})</button>`).join("")}
      ${xs.some((x) => !x.afiliado) ? `<span class="nota">${xs.filter((x) => !x.afiliado).length} sin Módulo 1 de Academia</span>` : ""}</div>
    <div id="tabla-referidos">${tablaReferidos(xs)}</div>`;
}
function tablaReferidos(xs) {
  const tot = xs.reduce((a, x) => a + x.beneficio, 0);
  return `<div class="tabla-scroll"><table><thead><tr><th>Referido</th><th>Inmobiliaria</th><th class="n">Nivel</th><th class="n">%</th><th>En la red desde</th><th class="n">Beneficio generado</th></tr></thead><tbody>
    ${xs.map((x) => `<tr><td>${esc(x.nombre)}${x.afiliado ? "" : ` <span class="chip vencido">No afiliado</span><span class="motivo">No ha completado el Módulo 1 de Academia, por lo que no forma parte del programa de referidos ni de Pulppo Plus. Impúlsalo a completarlo para que empiece a generarte beneficio.</span>`}</td>
      <td>${esc(x.inmobiliaria)}</td><td class="n">${x.nivel}</td><td class="n">${pct(x.porcentaje)}</td>
      <td>${x.desde ? fecha(x.desde) : "—"}</td><td class="n">${x.afiliado ? (x.beneficio ? mxn(x.beneficio) : `<span class="muted">${mxn(0)}</span>`) : `<span class="muted">—</span>`}</td></tr>`).join("")
      || `<tr><td colspan="6" class="muted">No hay referidos en este nivel.</td></tr>`}
    <tr class="total"><td colspan="5">Total</td><td class="n">${mxn(tot)}</td></tr></tbody></table></div>
    <p class="nota">Aparecen tus referidos activos que pueden generarte beneficio. Si alguno está en $0 es porque aún no cobra operaciones: cuando cierre, te genera beneficio.
    Los marcados como <b>No afiliado</b> todavía no completan el Módulo 1 de Academia: mientras no lo hagan, sus operaciones no te generan beneficio.</p>`;
}
function pintarReferidos() {
  document.querySelectorAll("#mis-referidos [data-nivel]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.nivel === NIVEL_REF));
  $("#tabla-referidos").innerHTML = tablaReferidos(REFERIDOS.filter((x) => !NIVEL_REF || String(x.nivel) === NIVEL_REF));
}

const stat = (v, l, s) => `<div class="stat"><div class="num">${mxn0(v)}</div><div class="lbl">${l}</div><div class="sub">${s}</div></div>`;

function avisosHTML(xs) {
  const nuevos = (xs || []).filter((x) => !x.leido);
  if (!nuevos.length) return "";
  return `<div class="avisos">${nuevos.map((x) => `<div class="caja ${/rechazada/.test(x.texto) ? "alerta" : "ok"} aviso"><p class="pre">${esc(x.texto)}</p><span class="fecha">${fecha(x.fecha)}</span></div>`).join("")}
    <div><button class="btn btn-sec btn-chico" id="avisos-ok">Entendido</button></div></div>`;
}

function formFactura(tipo, boton, corregir = "") {
  return `<form data-facturar="${tipo}" data-corregir="${esc(corregir)}" class="fila-form" style="margin-top:16px">
      <label class="campo"><span class="tag">Factura PDF</span><input type="file" name="pdf" accept="application/pdf,.pdf" required></label>
      <label class="campo"><span class="tag">Factura XML</span><input type="file" name="xml" accept=".xml,text/xml,application/xml" required></label>
      <button class="btn" type="submit">${boton}</button>
      <p class="nota error" data-error hidden></p>
    </form>`;
}

function porFacturarHTML(R, ro) {
  const v = R.por_facturar, partes = [];
  const meses = v.meses.map(mesTxt).join(", ");
  partes.push(`<header><h2>Por facturar</h2><span class="nota">Las facturas se suben del 1 al ${R.reglas.dia_limite} de cada mes. Lo que generas en un mes lo puedes facturar en las ventanas de los ${R.reglas.meses_para_cobrar} meses siguientes.</span></header>`);
  v.rechazadas.forEach((s) => partes.push(`<div class="caja alerta" style="margin-bottom:8px"><h3>Tu factura por ${mxn(s.monto)} fue rechazada</h3>
      <p class="pre"><b>Motivo:</b> ${esc(s.motivo_rechazo)}</p><p>Corrígela y vuelve a subirla a más tardar el <b>${fecha(s.limite_correccion)}</b>.</p>
      ${ro ? "" : formFactura("mensual", "Subir factura corregida", s.id)}</div>`));
  v.en_revision.forEach((s) => partes.push(`<div class="caja ${s.estado === "aprobado" ? "ok" : "acento-izq"}" style="margin-bottom:8px">${chip(s.estado)}
      <span class="monto-grande" style="display:inline-block;margin-left:12px;font-size:20pt">${mxn(s.monto)}</span>
      <p>${s.estado === "aprobado" ? `Aprobada. Se paga entre el ${fecha(s.pago_desde)} y el ${fecha(s.pago_hasta)}.` : "Recibimos tu factura. Finanzas y tu KAM la están revisando."}</p></div>`));
  if (v.monto) {
    partes.push(`<div class="caja acento-izq">
      <p class="tag">Disponible para facturar${meses ? ` · ${esc(meses)}` : ""}</p><div class="monto-grande">${mxn(v.monto)}</div>
      <p>Emite tu factura a Pulppo por <b>${mxn(v.monto)} más IVA</b> con el concepto <b>“Comisiones: programa referidos”</b> y súbela en PDF y XML
      a más tardar el <b>${fecha(v.ventana_cierra)}</b>.
      ${v.vence && v.vence === v.ventana_cierra ? `De ese monto, ${mxn(v.vence_monto)} ya no podrán esperar a la siguiente ventana.` : v.vence ? `Si no la subes en esta ventana, podrás hacerlo en la siguiente.` : ""}</p>
      ${ro ? "" : formFactura("mensual", "Enviar factura")}</div>`);
  } else if (v.proxima_monto) {
    partes.push(`<div class="caja acento-izq"><p class="tag">Listo para la próxima ventana</p><div class="monto-grande">${mxn(v.proxima_monto)}</div>
      <p>Podrás facturarlo del <b>${fecha(v.proxima_abre)}</b> al <b>${fecha(v.proxima_cierra)}</b>.${v.proxima_vence && v.proxima_vence === v.proxima_cierra ? " Es tu última oportunidad para parte de este monto." : ""}</p></div>`);
  } else if (!v.rechazadas.length && !v.en_revision.length) {
    partes.push(`<div class="caja"><p>No tienes montos disponibles para facturar por ahora.</p></div>`);
  }
  if (v.pendiente_regalia) {
    partes.push(`<p class="nota">${mxn(v.pendiente_regalia)} esperan a que la inmobiliaria de tu referido pague su regalía. En cuanto la pague, aparecerán aquí para facturar (ver “Detalle por mes”).</p>`);
  }
  return partes.join("");
}

async function subirFactura(e) {
  e.preventDefault();
  const f = e.target, b = f.querySelector("button"), err = f.querySelector("[data-error]");
  const fd = new FormData(f); fd.append("tipo", f.dataset.facturar);
  if (f.dataset.corregir) fd.append("corregir", f.dataset.corregir);
  b.disabled = true; b.textContent = "Enviando…"; err.hidden = true;
  try { await api("/api/facturar", { method: "POST", body: fd }); vistaAsesor(); }
  catch (x) { err.textContent = x.message; err.hidden = false; b.disabled = false; b.textContent = "Enviar factura"; }
}

function tablaSolicitudes(xs) {
  return `<div class="tabla-scroll"><table><thead><tr><th>Enviada en</th><th class="n">Monto</th><th>Estado</th><th>Enviada</th><th>Pago</th><th>Archivos</th></tr></thead><tbody>
    ${xs.map((s) => `<tr><td>${s.tipo === "anterior" ? "Programa anterior" : esc(mesTxt(s.periodo))}</td><td class="n">${mxn(s.monto)}</td>
      <td>${chip(s.estado)}${s.estado === "rechazado" ? `<span class="motivo">${esc(s.motivo_rechazo)}</span>` : ""}</td>
      <td>${fecha(s.creada)}</td>
      <td>${s.pagada ? `Pagada ${fecha(s.pagada)}` : s.pago_hasta ? `A más tardar ${fecha(s.pago_hasta)}` : "—"}</td>
      <td><a href="/archivo/${s.id}/pdf" target="_blank" rel="noopener">PDF</a> · <a href="/archivo/${s.id}/xml" target="_blank" rel="noopener">XML</a>${s.tiene_comprobante ? ` · <a href="/archivo/${s.id}/comprobante" target="_blank" rel="noopener">Comprobante</a>` : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
}

function detalleMeses(todas) {
  // Sin "No aplica" (excluidos por reglas); lo no cobrable se muestra como "No disponible"
  const lineas = todas.filter((l) => l.estado !== "excluido");
  if (!lineas.length) return `<p class="muted">Aún no hay comisiones de tus referidos en el programa relanzado.</p>`;
  const meses = {};
  lineas.forEach((l) => (meses[l.mes] ||= []).push(l));
  return Object.entries(meses).map(([m, xs], i) => {
    const tot = xs.reduce((s, l) => s + l.monto, 0);
    return `<details class="mes" ${i === 0 ? "open" : ""}><summary><span class="t">${esc(mesTxt(m))}</span><span class="muted">${xs.length} referido${xs.length === 1 ? "" : "s"} · tu nivel: ${esc(NIVEL[xs[0].nivel_referidor] || "—")}</span><span class="v">${mxn(tot)}</span></summary>
      <div class="tabla-scroll"><table><thead><tr><th>Referido</th><th>Inmobiliaria</th><th class="n">Nivel</th><th class="n">Comisión del referido</th><th class="n">%</th><th class="n">Beneficio</th><th>Estado</th></tr></thead><tbody>
      ${xs.map((l) => {
        const noDisp = l.grupo === "no_cobrable";
        return `<tr><td>${esc(l.referido)}</td><td>${esc(l.referido_inmobiliaria)}</td><td class="n">${l.nivel}</td>
        <td class="n">${mxn(l.base)}${l.base_elegible && l.base_elegible < l.base ? `<span class="motivo">tope: ${mxn(l.base_elegible)}</span>` : ""}</td>
        <td class="n">${pct(l.porcentaje)}</td><td class="n">${noDisp ? `<span class="muted">No disponible</span>` : mxn(l.monto)}</td>
        <td>${noDisp ? (l.motivo ? `<span class="motivo">${esc(l.motivo)}</span>` : "") : `${chip(l.estado)}${l.motivo ? `<span class="motivo">${esc(l.motivo)}</span>` : ""}`}</td></tr>`;
      }).join("")}
      </tbody></table></div></details>`;
  }).join("");
}

function anteriorHTML(A, ro) {
  const s = A.solicitud;
  let accion = "";
  if (s) {
    accion = s.estado === "rechazado"
      ? `<div class="caja alerta pre" style="margin-top:16px"><b>Rechazada:</b> ${esc(s.motivo_rechazo)} Corrige a más tardar el ${fecha(s.limite_correccion)}.${ro ? "" : formFactura("anterior", "Subir factura corregida", s.id)}</div>`
      : `<p style="margin-top:16px">${chip(s.estado)} Solicitud por ${mxn(s.monto)} enviada el ${fecha(s.creada)}.</p>`;
  } else if (A.disponible > 0 && A.abierto) {
    accion = `<div class="caja acento-izq" style="margin-top:16px"><p>Puedes cobrar <b>${mxn(A.disponible)}</b> hasta el <b>${fecha(A.limite)}</b>. Después se pierde.
      Factura por ese monto más IVA con el concepto “Comisiones: programa referidos”.</p>${ro ? "" : formFactura("anterior", "Enviar factura")}</div>`;
  }
  const pagos = A.pagos.length ? `<details class="mes"><summary><span class="t">Pagos registrados por Finanzas</span><span class="v">${mxn(A.cobrado)}</span></summary>
      <table><thead><tr><th>Concepto</th><th>Periodo</th><th>Emisión</th><th>Pago</th><th>Estado</th><th class="n">Total</th></tr></thead><tbody>
      ${A.pagos.map((p) => `<tr><td>${esc(p.concepto)}</td><td>${esc(p.periodo)}</td><td>${esc(p.emision)}</td><td>${esc(p.pago)}</td><td>${esc(p.status)}</td><td class="n">${mxn(p.total)}</td></tr>`).join("")}
      </tbody></table></details>` : "";
  return `<header><h2>Programa anterior</h2><span class="nota">Saldos generados antes del relanzamiento. No se recalculan.</span></header>
    <div class="stats">
      ${stat(A.disponible, "Disponible", `Cobrable hasta el ${fecha(A.limite)}`)}
      ${stat(A.en_proceso_plataforma + A.por_pagar_cxp, "En proceso", "Solicitado, por pagar")}
      ${stat(A.cobrado, "Cobrado", "Total facturado con IVA, según Finanzas")}
    </div>
    ${A.excluido_mb ? `<p class="nota">No incluye ${mxn(A.excluido_mb)} generados por referidos de tu propia inmobiliaria: como Master Broker no cobras por tu equipo.</p>` : ""}
    ${accion}${pagos}`;
}

function reglasHTML(r) {
  return `<ul class="reglas">
    <li>Cobras <b>3 %</b> de lo que generan tus referidos directos (nivel 1), <b>2 %</b> del nivel 2 y <b>1 %</b> del nivel 3.</li>
    <li>Se cuenta la comisión cobrada por tu referido en el mes: comprador 50 %, vendedor 25 % y productor 25 % cuando la operación es de ambos lados.</li>
    <li>Tope de ${mxn0(r.tope)} de comisión por referido cada 12 meses, contados desde que completa el Módulo 1 de Academia.</li>
    <li>Cobras el mes si eres <b>Profesional</b> (desde ${mxn0(r.umbral_profesional)} en 6 meses) o <b>Élite</b> (desde ${mxn0(r.umbral_elite)}). Si eres Estándar, ese mes se pierde.</li>
    <li>Solo cuenta si la inmobiliaria de tu referido pagó la regalía del mes.</li>
    <li>Si eres Master Broker, no cobras por los asesores de tu propia inmobiliaria.</li>
    <li>Las facturas se suben del 1 al ${r.dia_limite} de cada mes. Lo que generas en un mes lo puedes facturar en las ventanas de los ${r.meses_para_cobrar} meses siguientes (lo de octubre, del 1 al ${r.dia_limite} de noviembre o del 1 al ${r.dia_limite} de diciembre). Después se pierde.</li>
    <li>Una vez aprobada tu factura, se paga en ${r.pago_min} a ${r.pago_max} días.</li></ul>`;
}

// ---------------------------------------------------------------- admins: bandeja

async function vistaBandeja() {
  $("#app").innerHTML = `<p class="muted">Cargando facturas…</p>`;
  SOLS = await api("/api/admin/solicitudes");
  pintarBandeja();
}

function pintarBandeja() {
  const n = (e) => SOLS.filter((s) => s.estado === e).length;
  const vencidas = SOLS.filter((s) => s.estado === "aprobado" && s.dias_para_pagar < 0).length;
  const filtros = [["facturado", "Por revisar"], ["aprobado", "Por pagar"], ["rechazado", "Rechazadas"], ["pagado", "Pagadas"], ["", "Todas"]];
  const q = ($("#buscar")?.value || "").toLowerCase();
  const xs = SOLS.filter((s) => (!FILTRO || s.estado === FILTRO) && (!q || `${s.referidor_nombre} ${s.inmobiliaria} ${s.referidor_email}`.toLowerCase().includes(q)));
  const totalPorPagar = SOLS.filter((s) => s.estado === "aprobado").reduce((a, s) => a + s.monto, 0);
  $("#app").innerHTML = `
    <header class="encabezado"><p class="eyebrow">${YO.finanzas ? "Finanzas" : "KAM"}</p><h1>Facturas de referidos</h1>
      <div class="meta"><span>Por revisar: <b>${n("facturado")}</b></span><span>Por pagar: <b>${n("aprobado")} · ${mxn0(totalPorPagar)}</b></span>
      ${vencidas ? `<span class="rojo">Pago vencido: <b class="rojo">${vencidas}</b></span>` : ""}</div><div class="acento"></div></header>
    <div class="filtros">${filtros.map(([k, l]) => `<button class="filtro" data-f="${k}" aria-pressed="${FILTRO === k}">${l}${k ? ` (${n(k)})` : ""}</button>`).join("")}
      <input type="search" id="buscar" placeholder="Buscar asesor o inmobiliaria" value="${esc(q)}"></div>
    <div class="tabla-scroll"><table><thead><tr><th>Asesor</th><th>Inmobiliaria</th><th>Enviada en</th><th class="n">Monto</th><th>Estado</th><th>Actualizada</th><th>Pagar a más tardar</th></tr></thead><tbody>
    ${xs.map((s) => `<tr class="click ${SEL === s.id ? "sel" : ""}" data-id="${s.id}"><td>${esc(s.referidor_nombre)}<span class="motivo">${esc(s.referidor_email)}</span></td>
      <td>${esc(s.inmobiliaria)}</td><td>${s.tipo === "anterior" ? "Programa anterior" : esc(mesTxt(s.periodo))}</td><td class="n">${mxn(s.monto)}</td><td>${chip(s.estado)}</td>
      <td>${fecha(s.actualizada)}</td><td class="${s.dias_para_pagar < 0 ? "rojo" : ""}">${s.estado === "aprobado" ? `${fecha(s.pago_hasta)} (${s.dias_para_pagar < 0 ? `vencido hace ${-s.dias_para_pagar} d` : `${s.dias_para_pagar} d`})` : "—"}</td></tr>`).join("")
      || `<tr><td colspan="7" class="muted">No hay facturas con este filtro.</td></tr>`}
    </tbody></table></div>
    <div id="detalle"></div>`;
  if (SEL) verSolicitud(SEL);
}

document.addEventListener("click", (e) => {
  const f = e.target.closest("[data-f]");
  if (f) { FILTRO = f.dataset.f; SEL = null; pintarBandeja(); return; }
  const tr = e.target.closest("tr.click[data-id]");
  if (tr) { SEL = tr.dataset.id; document.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel")); tr.classList.add("sel"); verSolicitud(SEL); }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "buscar") { const pos = e.target.selectionStart; pintarBandeja(); const b = $("#buscar"); b.focus(); b.setSelectionRange(pos, pos); }
  if (e.target.id === "buscar-ref") pintarReferidores();
});

async function verSolicitud(id) {
  const box = $("#detalle");
  box.innerHTML = `<div class="detalle"><p class="muted">Cargando…</p></div>`;
  const s = await api(`/api/admin/solicitud/${id}`);
  const acciones = [];
  if (s.estado === "facturado") acciones.push(`<button class="btn" data-acc="aprobar">Aprobar</button>`, `<button class="btn btn-rojo" data-acc="rechazar">Rechazar</button>`);
  if (s.estado === "aprobado" && YO.finanzas) acciones.push(`<button class="btn" data-acc="pagar">Marcar como pagado</button>`, `<button class="btn btn-sec" data-acc="revertir">Regresar a revisión</button>`);
  const c = s.cfdi || {};
  box.innerHTML = `<div class="detalle">
    <header class="bloque-h"><p class="eyebrow">${s.tipo === "anterior" ? "Programa anterior" : `Enviada en ${esc(mesTxt(s.periodo))}`}</p>
      <h2>${esc(s.referidor_nombre)} · ${mxn(s.monto)}</h2>
      <div class="meta"><span>${esc(s.inmobiliaria)}</span><span>${esc(s.referidor_email)}</span><span>${chip(s.estado)}</span>
      <span><a href="/archivo/${s.id}/pdf" target="_blank" rel="noopener">Ver PDF</a> · <a href="/archivo/${s.id}/xml" target="_blank" rel="noopener">Ver XML</a>${s.tiene_comprobante ? ` · <a href="/archivo/${s.id}/comprobante" target="_blank" rel="noopener">Comprobante</a>` : ""}</span></div></header>
    ${s.estado === "rechazado" ? `<div class="caja alerta pre" style="margin-top:16px"><b>Motivo del rechazo:</b> ${esc(s.motivo_rechazo)}<br>Plazo para corregir: ${fecha(s.limite_correccion)}</div>` : ""}
    ${s.estado === "aprobado" ? `<div class="caja ${s.dias_para_pagar < 0 ? "alerta" : "acento-izq"}" style="margin-top:16px">Aprobada el ${fecha(s.aprobada)}. Pagar entre el <b>${fecha(s.pago_desde)}</b> y el <b>${fecha(s.pago_hasta)}</b>.</div>` : ""}
    ${s.estado === "pagado" ? `<div class="caja ok" style="margin-top:16px">Pagada el ${fecha(s.pagada)}${s.pago_referencia ? ` · ${esc(s.pago_referencia)}` : ""}.</div>` : ""}
    <div class="acciones">${acciones.join("")}</div>
    <div class="grid2">
      <div class="caja"><h3>Revisión de la factura</h3>
        <p class="nota">Emisor: ${esc(c.EmisorNombre || "")} ${esc(c.EmisorRfc || "")} · Receptor: ${esc(c.ReceptorNombre || "")} ${esc(c.ReceptorRfc || "")}<br>
        Subtotal ${c.SubTotal != null ? mxn(c.SubTotal) : "—"} · Total ${c.Total != null ? mxn(c.Total) : "—"} · Concepto: ${esc(c.Concepto || "—")}</p>
        <ul class="checks">${s.revision.map((x) => `<li class="${x.ok ? "" : "mal"}">${esc(x.texto)}</li>`).join("")}</ul></div>
      <div class="caja"><h3>Historial</h3><ul class="historial">${s.eventos.map((e) => `<li>${fecha(e.fecha)} · ${esc(e.actor_email)} (${esc(e.actor_rol)}): ${esc(e.de ? `${ESTADO[e.de] || e.de} → ` : "")}${esc(ESTADO[e.a] || e.a)}${e.nota ? ` — ${esc(e.nota)}` : ""}</li>`).join("")}</ul></div>
    </div>
    <h3 style="margin:24px 0 8px">Qué incluye</h3>
    <div class="tabla-scroll"><table><thead><tr><th>Mes</th><th>Referido</th><th class="n">Nivel</th><th class="n">Base</th><th class="n">Beneficio</th></tr></thead><tbody>
      ${s.detalle.map((l) => `<tr><td>${esc(l.mes ? (l.mes.length === 7 ? mesTxt(l.mes) : l.mes) : "")}</td><td>${esc(l.referido || "")}${l.operacion ? `<span class="motivo">${esc(l.operacion)}</span>` : ""}</td><td class="n">${esc(l.nivel ?? "")}</td><td class="n">${l.base_elegible != null ? mxn(l.base_elegible) : "—"}</td><td class="n">${mxn(l.monto)}</td></tr>`).join("")}
      <tr class="total"><td colspan="4">Total</td><td class="n">${mxn(s.monto)}</td></tr></tbody></table></div>
    </div>`;
  box.querySelectorAll("[data-acc]").forEach((b) => b.addEventListener("click", () => accion(s, b.dataset.acc)));
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

function dialogo(html, alEnviar) {
  const d = $("#dlg");
  d.innerHTML = `<form method="dialog">${html}<p class="nota error" data-error hidden></p><div class="botones"><button type="button" class="btn btn-sec" value="cancel">Cancelar</button><button class="btn" type="submit">Confirmar</button></div></form>`;
  const f = d.querySelector("form");
  f.querySelector('[value="cancel"]').addEventListener("click", () => d.close());
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = f.querySelector("[data-error]"), b = f.querySelector('[type="submit"]');
    b.disabled = true;
    try { await alEnviar(f); d.close(); } catch (x) { err.textContent = x.message; err.hidden = false; b.disabled = false; }
  });
  d.showModal();
  const t = f.querySelector("textarea, input");
  t?.focus();
  if (t?.tagName === "TEXTAREA") { t.setSelectionRange(t.value.length, t.value.length); t.scrollTop = t.scrollHeight; }
}

async function accion(s, acc) {
  const recargar = async () => { SOLS = await api("/api/admin/solicitudes"); pintarBandeja(); };
  if (acc === "aprobar") {
    dialogo(`<h3>Aprobar factura</h3><p>${esc(s.referidor_nombre)} · ${mxn(s.monto)}</p><p class="nota">Se le avisa al asesor y empieza a correr el plazo de pago de 30 a 45 días.</p>`,
      async () => { await post(`/api/admin/solicitud/${s.id}/aprobar`); await recargar(); });
  } else if (acc === "rechazar") {
    const detectado = (s.revision || []).filter((x) => !x.ok && x.motivo).map((x) => `• ${x.motivo}`);
    const pre = detectado.length ? detectado.join("\n") + "\n" : "";
    dialogo(`<h3>Rechazar factura</h3><label class="campo"><span class="tag">Motivo del rechazo (lo verá el asesor)</span>
      <textarea name="motivo" rows="6" required minlength="10" placeholder="Ej.: el subtotal no coincide con el monto; el RFC del receptor es incorrecto…">${esc(pre)}</textarea></label>
      <p class="nota">${detectado.length ? "Precargado con lo que detectó la revisión. Puedes editarlo y agregar tus comentarios debajo. " : ""}Le llega un aviso y un correo con el motivo para que corrija su factura.</p>`,
      async (f) => { await post(`/api/admin/solicitud/${s.id}/rechazar`, { motivo: f.motivo.value }); await recargar(); });
  } else if (acc === "pagar") {
    const hoy = YO.hoy;
    dialogo(`<h3>Marcar como pagado</h3><p>${esc(s.referidor_nombre)} · ${mxn(s.monto)}</p>
      <label class="campo"><span class="tag">Fecha de pago</span><input type="date" name="fecha" value="${hoy}" required></label>
      <label class="campo"><span class="tag">Referencia (SPEI, folio)</span><input name="referencia"></label>
      <label class="campo"><span class="tag">Comprobante (opcional)</span><input type="file" name="comprobante" accept=".pdf,image/*"></label>`,
      async (f) => { await api(`/api/admin/solicitud/${s.id}/pagar`, { method: "POST", body: new FormData(f) }); await recargar(); });
  } else if (acc === "revertir") {
    dialogo(`<h3>Regresar a revisión</h3><p class="nota">Quita la aprobación (por ejemplo, si se aprobó por error). Vuelve a "Por revisar".</p>`,
      async () => { await post(`/api/admin/solicitud/${s.id}/revertir`); await recargar(); });
  }
}

// ---------------------------------------------------------------- admins: desglose de beneficios

let BEN = null, BEN_F = { grupo: "", mes: "", q: "" };
const GRUPOS = { a_cobrar: "Por facturar", en_proceso: "En proceso", cobrado: "Cobrado", a_futuro: "A futuro", no_cobrable: "No cobrable", no_aplica: "No aplica" };
const grupoDe = (l) => (l.estado === "excluido" ? "no_aplica" : l.grupo);  // "No aplica" = la regla lo excluye (MB propia inmo, sin Módulo 1)

async function vistaBeneficios() {
  $("#app").innerHTML = `<p class="muted">Calculando beneficios…</p>`;
  BEN = await api("/api/admin/beneficios");
  const meses = [...new Set(BEN.lineas.map((l) => l.mes))].sort().reverse();
  const bl = BEN.bloqueos;
  $("#app").innerHTML = `<header class="encabezado"><p class="eyebrow">Solo admins</p><h1>Beneficios generados</h1>
      <p class="nota">Todo lo que generó cada referidor, con su estado y el motivo cuando no se puede cobrar. Lo generado en un mes se factura en las ventanas (1 al 10) de los ${BEN.meses_para_cobrar} meses siguientes.</p><div class="acento"></div></header>
    <section class="bloque"><header><h2>Cierres de regalías que detienen beneficios</h2>
      <span class="nota">Doble check: si la inmobiliaria ya pagó, marca su cierre del mes como pagado en la plataforma y el beneficio se libera.</span></header>
      ${bl.length ? `<div class="tabla-scroll"><table><thead><tr><th>Inmobiliaria del referido</th><th>Mes</th><th>Cierre</th><th>Referidores afectados</th><th class="n">Detenido</th><th class="n">Ya no cobrable</th><th>Se puede facturar hasta</th></tr></thead><tbody>
      ${bl.map((b) => `<tr><td>${esc(b.inmobiliaria)}</td><td>${esc(mesTxt(b.mes))}</td><td>${esc({ pending: "Pendiente", expired: "Vencido", sin_cierre: "Sin cierre", paid: "Pagado tarde" }[b.regalia] || b.regalia)}</td>
        <td>${esc(b.referidores.join(", "))}</td><td class="n">${b.pendiente ? mxn(b.pendiente) : "—"}</td><td class="n ${b.perdido ? "rojo" : ""}">${b.perdido ? mxn(b.perdido) : "—"}</td>
        <td>${b.perdido ? `<span class="rojo">Terminó</span>` : fecha(b.cobro_hasta)}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">No hay beneficios detenidos por regalías.</p>`}
    </section>
    <section class="bloque"><header><h2>Desglose por referidor</h2></header>
      <div class="filtros">
        ${[["", "Todos"], ...Object.entries(GRUPOS)].map(([k, l]) => `<button class="filtro" data-grupo="${k}" aria-pressed="${BEN_F.grupo === k}">${l}</button>`).join("")}
        <select id="ben-mes"><option value="">Todos los meses</option>${meses.map((m) => `<option value="${m}" ${BEN_F.mes === m ? "selected" : ""}>${esc(mesTxt(m))}</option>`).join("")}</select>
        <input type="search" id="ben-q" placeholder="Buscar referidor, referido o inmobiliaria" value="${esc(BEN_F.q)}">
      </div><div id="ben-tabla"></div></section>`;
  $("#app").addEventListener("click", (e) => { const b = e.target.closest("[data-grupo]"); if (b) { BEN_F.grupo = b.dataset.grupo; document.querySelectorAll("[data-grupo]").forEach((x) => x.setAttribute("aria-pressed", x.dataset.grupo === BEN_F.grupo)); pintarBeneficios(); } });
  $("#ben-mes").addEventListener("change", (e) => { BEN_F.mes = e.target.value; pintarBeneficios(); });
  $("#ben-q").addEventListener("input", (e) => { BEN_F.q = e.target.value; pintarBeneficios(); });
  pintarBeneficios();
}

function pintarBeneficios() {
  const q = BEN_F.q.toLowerCase();
  const xs = BEN.lineas.filter((l) => (!BEN_F.grupo || grupoDe(l) === BEN_F.grupo) && (!BEN_F.mes || l.mes === BEN_F.mes)
    && (!q || `${l.referidor} ${l.referido} ${l.referido_inmobiliaria} ${l.referidor_inmobiliaria}`.toLowerCase().includes(q)));
  const por = {};
  xs.forEach((l) => (por[l.referidor_id] ||= { nombre: l.referidor, inmo: l.referidor_inmobiliaria, tipo: l.referidor_tipo, xs: [] }).xs.push(l));
  const grupos = Object.values(por).sort((a, b) => a.nombre.localeCompare(b.nombre, "es"));
  const suma = (ls, g) => ls.filter((l) => grupoDe(l) === g).reduce((a, l) => a + (g === "no_cobrable" || g === "no_aplica" ? l.monto_sin_reglas : l.monto), 0);
  $("#ben-tabla").innerHTML = grupos.length ? grupos.map((g) => `<details class="mes"><summary><span class="t">${esc(g.nombre)}</span>
      <span class="muted">${esc(g.inmo)}${g.tipo === "master" ? " · Master Broker" : ""} · ${g.xs.length} línea${g.xs.length === 1 ? "" : "s"}</span>
      <span class="v">${Object.entries(GRUPOS).filter(([k]) => suma(g.xs, k)).map(([k, l]) => `${l}: ${mxn0(suma(g.xs, k))}`).join(" · ")}</span></summary>
      <div class="tabla-scroll"><table><thead><tr><th>Mes</th><th>Referido</th><th>Inmobiliaria</th><th class="n">Nivel</th><th class="n">Comisión</th><th class="n">%</th><th class="n">Beneficio</th><th>Estado</th></tr></thead><tbody>
      ${g.xs.sort((a, b) => b.mes.localeCompare(a.mes) || a.referido.localeCompare(b.referido, "es")).map((l) => `<tr><td>${esc(mesTxt(l.mes))}</td><td>${esc(l.referido)}</td><td>${esc(l.referido_inmobiliaria)}</td>
        <td class="n">${l.nivel}</td><td class="n">${mxn(l.base)}</td><td class="n">${pct(l.porcentaje)}</td>
        <td class="n">${l.grupo === "no_cobrable" ? `<span class="muted">${mxn(l.monto_sin_reglas)}</span>` : mxn(l.monto)}</td>
        <td>${grupoDe(l) === "no_aplica" ? `<span class="chip">No aplica</span>` : l.grupo === "no_cobrable" ? `<span class="chip vencido">No cobrable</span>` : chip(l.estado)}${l.motivo ? `<span class="motivo ${l.por_regalia ? "rojo" : ""}">${esc(l.motivo)}</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div></details>`).join("") : `<p class="muted">No hay beneficios con este filtro.</p>`;
}

// ---------------------------------------------------------------- admins: referidores

let REFS = [], ORDEN = { campo: "monto", dir: 1 };
const ORDENES = { monto: "Mayor monto por cobrar", nombre: "Referidor (A–Z)", inmobiliaria: "Inmobiliaria (A–Z)", referidos: "Más referidos" };
async function vistaReferidores() {
  $("#app").innerHTML = `<p class="muted">Calculando beneficios de todos los referidores…</p>`;
  REFS = await api("/api/admin/referidores");
  $("#app").innerHTML = `<header class="encabezado"><p class="eyebrow">Programa relanzado + anterior</p><h1>Referidores</h1>
      <div class="meta"><span>${REFS.length} referidores activos</span><button class="link" id="actualizar">Actualizar datos de Mongo</button></div><div class="acento"></div></header>
    <div class="filtros"><label class="campo"><span class="tag">Ordenar por</span><select id="orden-ref">${Object.entries(ORDENES).map(([k, l]) => `<option value="${k}" ${ORDEN.campo === k ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <input type="search" id="buscar-ref" placeholder="Buscar asesor o inmobiliaria"></div><div id="tabla-ref"></div>`;
  $("#orden-ref").addEventListener("change", (e) => { ORDEN = { campo: e.target.value, dir: 1 }; pintarReferidores(); });
  $("#actualizar").addEventListener("click", async (e) => { e.target.textContent = "Actualizando…"; await post("/api/admin/actualizar"); vistaReferidores(); });
  pintarReferidores();
}

function pintarReferidores() {
  const q = ($("#buscar-ref")?.value || "").toLowerCase();
  const txt = (s) => (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  const cmp = {
    monto: (a, b) => (b.a_cobrar + b.a_futuro + b.anterior_disponible) - (a.a_cobrar + a.a_futuro + a.anterior_disponible) || txt(a.nombre).localeCompare(txt(b.nombre), "es"),
    nombre: (a, b) => txt(a.nombre).localeCompare(txt(b.nombre), "es"),
    inmobiliaria: (a, b) => txt(a.inmobiliaria).localeCompare(txt(b.inmobiliaria), "es") || txt(a.nombre).localeCompare(txt(b.nombre), "es"),
    referidos: (a, b) => b.referidos - a.referidos || txt(a.nombre).localeCompare(txt(b.nombre), "es"),
  }[ORDEN.campo];
  const xs = REFS.filter((r) => !q || `${r.nombre} ${r.email} ${r.inmobiliaria}`.toLowerCase().includes(q)).sort((a, b) => ORDEN.dir * cmp(a, b));
  const flecha = (k) => (ORDEN.campo === k ? (ORDEN.dir === 1 ? " ↑" : " ↓") : "");
  const sum = (k) => xs.reduce((a, r) => a + r[k], 0);
  $("#tabla-ref").innerHTML = `<div class="tabla-scroll"><table><thead><tr><th><button class="orden" data-orden="nombre">Asesor${flecha("nombre")}</button></th><th><button class="orden" data-orden="inmobiliaria">Inmobiliaria${flecha("inmobiliaria")}</button></th><th class="n"><button class="orden" data-orden="referidos">Referidos${flecha("referidos")}</button></th><th class="n">A cobrar</th><th class="n">En proceso</th><th class="n">Cobrado</th><th class="n">A futuro</th><th class="n">Anterior disponible</th><th></th></tr></thead><tbody>
    ${xs.slice(0, 300).map((r) => `<tr><td>${esc(r.nombre)}<span class="motivo">${esc(r.email)}${r.tipo === "master" ? " · Master Broker" : ""}</span></td><td>${esc(r.inmobiliaria)}</td>
      <td class="n">${r.referidos}</td><td class="n">${mxn0(r.a_cobrar)}</td><td class="n">${mxn0(r.en_proceso)}</td><td class="n">${mxn0(r.cobrado)}</td><td class="n">${mxn0(r.a_futuro)}</td>
      <td class="n">${mxn0(r.anterior_disponible)}${r.anterior_excluido_mb ? `<span class="motivo">sin ${mxn0(r.anterior_excluido_mb)} propia inmo</span>` : ""}</td>
      <td><button class="btn btn-sec btn-chico" data-como="${r.id}">Ver como asesor</button></td></tr>`).join("")}
    <tr class="total"><td colspan="3">Total (${xs.length})</td><td class="n">${mxn0(sum("a_cobrar"))}</td><td class="n">${mxn0(sum("en_proceso"))}</td><td class="n">${mxn0(sum("cobrado"))}</td><td class="n">${mxn0(sum("a_futuro"))}</td><td class="n">${mxn0(sum("anterior_disponible"))}</td><td></td></tr>
    </tbody></table></div>${xs.length > 300 ? `<p class="nota">Se muestran 300 de ${xs.length}; usa el buscador.</p>` : ""}`;
  document.querySelectorAll("[data-orden]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.orden;
    ORDEN = ORDEN.campo === k ? { campo: k, dir: -ORDEN.dir } : { campo: k, dir: 1 };
    $("#orden-ref").value = k; pintarReferidores();
  }));
  document.querySelectorAll("[data-como]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.setAttribute("aria-selected", "false"));
    vistaAsesor(b.dataset.como); window.scrollTo(0, 0);
  }));
}

// ---------------------------------------------------------------- admins: simulación

async function vistaSimulacion(desde = "2026-01") {
  $("#app").innerHTML = `<p class="muted">Simulando las reglas nuevas…</p>`;
  const S = await api(`/api/admin/simulacion?desde=${desde}`);
  const cols = [["sin_reglas", "Sin reglas nuevas"], ["excluido_mb", "MB propia inmo"], ["estandar", "Referidor Estándar"], ["modulo1", "Sin Módulo 1"], ["regalia", "Regalía no pagada"], ["tope", "Tope"], ["pendiente", "Pendiente"], ["a_pagar", "A pagar"]];
  const tot = (k) => S.meses.reduce((a, m) => a + m[k], 0);
  $("#app").innerHTML = `<header class="encabezado"><p class="eyebrow">Solo admins</p><h1>Simulación de reglas nuevas</h1>
      <p class="nota">Qué se habría pagado si las reglas nuevas aplicaran desde ${esc(mesTxt(desde))}, sin plazos de cobro. ${esc(S.nota)}</p><div class="acento"></div></header>
    <div class="filtros"><label class="campo"><span class="tag">Desde</span><input type="month" id="sim-desde" value="${desde}"></label></div>
    <div class="tabla-scroll"><table><thead><tr><th>Mes</th>${cols.map(([, l]) => `<th class="n">${l}</th>`).join("")}</tr></thead><tbody>
    ${S.meses.map((m) => `<tr><td>${esc(mesTxt(m.mes))}</td>${cols.map(([k]) => `<td class="n">${mxn0(m[k])}</td>`).join("")}</tr>`).join("")}
    <tr class="total"><td>Total</td>${cols.map(([k]) => `<td class="n">${mxn0(tot(k))}</td>`).join("")}</tr></tbody></table></div>
    <p class="nota">“Sin reglas nuevas” = 3/2/1 % sobre toda la comisión de los referidos. Las demás columnas muestran cuánto quita cada regla. “Tope” = lo que no se paga por el límite de $600,000.</p>`;
  $("#sim-desde").addEventListener("change", (e) => e.target.value && vistaSimulacion(e.target.value));
}

// ---------------------------------------------------------------- admins: excepciones

async function vistaExcepciones() {
  const xs = await api("/api/admin/excepciones");
  $("#app").innerHTML = `<header class="encabezado"><p class="eyebrow">Master Brokers</p><h1>Excepciones</h1>
      <p class="nota">Cobran sin depender de su nivel Pulppo Plus y acumulan su beneficio. Siguen sin cobrar por su propia inmobiliaria. Las propone Pulppo.</p><div class="acento"></div></header>
    <table><thead><tr><th>Correo</th><th>Agregada por</th><th>Fecha</th><th></th></tr></thead><tbody>
    ${xs.map((x) => `<tr><td>${esc(x.email)}</td><td>${esc(x.agregada_por)}</td><td>${fecha(x.fecha)}</td><td>${YO.finanzas ? `<button class="btn btn-sec btn-chico" data-quitar="${esc(x.email)}">Quitar</button>` : ""}</td></tr>`).join("")}
    </tbody></table>
    ${YO.finanzas ? `<form class="fila-form" id="form-exc" style="margin-top:24px"><label class="campo"><span class="tag">Correo del Master Broker</span><input type="email" id="exc-email" required></label><button class="btn">Agregar</button></form>` : ""}`;
  $("#form-exc")?.addEventListener("submit", async (e) => { e.preventDefault(); await post("/api/admin/excepciones", { email: $("#exc-email").value }); vistaExcepciones(); });
  document.querySelectorAll("[data-quitar]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`¿Quitar la excepción de ${b.dataset.quitar}?`)) return;
    await api(`/api/admin/excepciones/${encodeURIComponent(b.dataset.quitar)}`, { method: "DELETE" }); vistaExcepciones();
  }));
}

iniciar();
