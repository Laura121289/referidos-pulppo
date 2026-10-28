"""Herramienta de beneficios de Pulppo: Programa Referidos y Bono 1·5·10 (inicio con una tarjeta por programa).

Referidos. Asesor: ve sus beneficios (a cobrar, en proceso, cobrados, a futuro), sube su factura del 1 al 10 y ve el estado.
Bono 1·5·10 (bono.py). Cualquier bróker con correo @pulppo.com: ve el bono de las operaciones donde fue comprador y sube
una factura por operación. Los mismos admins de referidos.
Finanzas y KAMs (admins): bandeja de facturas de cada programa → aprobar / rechazar con motivo; Finanzas marca como pagado.

Local:   ../../.venv/bin/python app.py  →  http://127.0.0.1:8060  (entrar con /auth/local?email=...; HOY=AAAA-MM-DD simula la fecha)
Vercel:  preparar_deploy.py arma deploy/ (ver CLAUDE.md del subproyecto).
"""
import json
import os
import secrets
import sys
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, session

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parents[1] / "base-de-datos" / "lib"):
    if p.exists():
        sys.path.insert(0, str(p))

import almacen  # noqa: E402
import bono  # noqa: E402
import calculo  # noqa: E402
from almacen import ErrorFlujo  # noqa: E402

EN_VERCEL = bool(os.environ.get("VERCEL"))
DS = HERE / "ds" if (HERE / "ds").exists() else HERE.parents[1] / "design-system" / "project"
CFG = json.loads((HERE / "config.json").read_text())
if not EN_VERCEL and os.environ.get("INICIO_PROGRAMA"):
    CFG["inicio_programa"] = os.environ["INICIO_PROGRAMA"]  # solo local, para probar con meses ya cerrados
if not EN_VERCEL and os.environ.get("BONO_DESDE"):
    CFG["bono_desde"] = os.environ["BONO_DESDE"]  # solo local, para probar el bono con cierres anteriores
CACHE_TTL = 600


def _secreto(nombre):
    if os.environ.get(nombre):
        return os.environ[nombre]
    if EN_VERCEL:
        raise RuntimeError(f"Falta la variable de entorno {nombre}")
    f = HERE / ".secretos.json"
    d = json.loads(f.read_text()) if f.exists() else {}
    if nombre not in d:
        d[nombre] = secrets.token_urlsafe(32)
        f.write_text(json.dumps(d))
        f.chmod(0o600)
    return d[nombre]


BROKER_SSO_URL = os.environ.get("BROKER_SSO_URL", "")  # p. ej. https://broker.pulppo.com/referidos-sso
PULPPO_API_URL = os.environ.get("PULPPO_API_URL", "https://api.pulppo.com")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
app = Flask(__name__, static_folder=None)
app.secret_key = _secreto("SECRET_KEY")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=EN_VERCEL,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30), MAX_CONTENT_LENGTH=12 * 1024 * 1024)


def _ruta_original(wsgi):
    """vercel.json manda todo a /api/index?__ruta=/<ruta original>: devolverle a Flask la ruta original."""
    from urllib.parse import parse_qsl, urlencode

    def envuelto(environ, start_response):
        if environ.get("PATH_INFO", "").startswith("/api/index"):
            qs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            ruta = next((v for k, v in qs if k == "__ruta"), None)
            if ruta is not None:
                environ["PATH_INFO"] = ruta if ruta.startswith("/") else "/" + ruta
                environ["QUERY_STRING"] = urlencode([(k, v) for k, v in qs if k != "__ruta"])
        return wsgi(environ, start_response)
    return envuelto


app.wsgi_app = _ruta_original(app.wsgi_app)
almacen.iniciar(CFG)


def hoy():
    if not EN_VERCEL and os.environ.get("HOY"):
        return date.fromisoformat(os.environ["HOY"])  # solo local, para probar ventanas de factura
    return datetime.now(calculo.MX).date()


def mes_hoy():
    h = hoy()
    return f"{h.year:04d}-{h.month:02d}"


# ---------------------------------------------------------------- datos (Mongo con caché)

_cache = {}


def _cacheado(clave, fn, fresco=False):
    v = _cache.get(clave)
    if v and not fresco and time.time() - v[0] < CACHE_TTL:
        return v[1]
    r = fn()
    _cache[clave] = (time.time(), r)
    return r


def db():
    from pulppo_db import get_db
    return get_db()


def datos(desde=None, fresco=False):
    desde = desde or CFG["inicio_programa"]
    hasta = max(mes_hoy(), desde)
    d = _cacheado(("datos", desde, hasta), lambda: calculo.cargar(db(), desde, hasta, CFG["inmobiliarias_excluidas"]), fresco)
    vistas = almacen.registrar_regalias(d["regalias"], hoy())
    for k, v in d["regalias"].items():
        v["pagada_en"] = vistas.get(k)
    return d


def lineas_programa():
    sols = [s for s in almacen.solicitudes(tipo="mensual")]
    return calculo.calcular(datos(), CFG, hoy(), sols, almacen.excepciones())


def anterior():
    return _cacheado("anterior", lambda: calculo.programa_anterior(db(), CFG["inmobiliarias_excluidas"]))


def lineas_bono():
    ops = _cacheado("bono", lambda: bono.cargar(db(), CFG, CFG["inmobiliarias_excluidas"]))
    return bono.calcular(ops, hoy(), almacen.solicitudes(tipo="bono"))


def cxp():
    f = HERE / "historico_cxp.json"
    return json.loads(f.read_text()) if f.exists() else []


def agente_por_email(email):
    a = db().agents.find_one({"email": email, "deletedAt": None},
                             {"firstName": 1, "lastName": 1, "email": 1, "type": 1, "status": 1, "company": 1, "uid": 1,
                              "courses": 1, "createdAt": 1, "level": 1})
    if not a:
        return None
    m1 = [c["_id"] for c in db().courses.find({"level": {"$lte": 1}, "isActive": True}, {"_id": 1})]
    return calculo._agente(a, m1)


def agente_por_id(aid):
    from bson import ObjectId
    d = datos()
    if aid in d["agentes"]:
        return d["agentes"][aid]
    a = db().agents.find_one({"_id": ObjectId(aid)}, {"email": 1})
    return agente_por_email(a["email"]) if a else None


# ---------------------------------------------------------------- roles

def kams():
    f = HERE / "kams.json"
    return json.loads(f.read_text()) if f.exists() else []


def finanzas():
    extra = [e.strip().lower() for e in os.environ.get("FINANZAS_EMAILS", "").split(",") if e.strip()]
    return {e.lower() for e in CFG.get("finanzas", [])} | set(extra)


def roles_de(email):
    roles = []
    if email in finanzas():
        roles.append("finanzas")
    if any((k.get("email") or "").lower() == email for k in kams()):
        roles.append("kam")
    return roles


def inmos_del_kam(email):
    """Inmobiliarias que ve el KAM; None = todas (KAM sin lista de inmobiliarias en kams.json)."""
    out = set()
    for k in kams():
        if (k.get("email") or "").lower() == email:
            if not k.get("inmobiliarias"):
                return None
            out |= {calculo.norm_company(n) for n in k["inmobiliarias"]}
    return out


def ve_inmo(u, nombre):
    if "finanzas" in u.get("roles", []):
        return True
    inmos = inmos_del_kam(u["email"]) if "kam" in u.get("roles", []) else set()
    return inmos is None or calculo.norm_company(nombre) in inmos


def usuario():
    return session.get("usuario")


def es_broker_bono(email):
    return email.lower().endswith("@" + CFG["bono_dominio"])


def programas(u):
    """Programas que ve el usuario: referidos (asesores en Mongo y admins) y bono (correo @pulppo.com y admins)."""
    out = []
    if u.get("agente_id") or u["roles"]:
        out.append("referidos")
    if (u.get("agente_id") and es_broker_bono(u["email"])) or u["roles"]:
        out.append("bono")
    return out


def requiere(*roles):
    def deco(fn):
        @wraps(fn)
        def envuelto(*a, **kw):
            u = usuario()
            if not u:
                abort(401)
            if roles and not set(roles) & set(u.get("roles", [])):
                abort(403)
            return fn(*a, **kw)
        return envuelto
    return deco


def _entrar(email, nombre=None):
    email = email.lower()
    ag = agente_por_email(email)
    roles = roles_de(email)
    if not ag and not roles:
        return None
    session.clear()
    session.permanent = True
    session["usuario"] = {"email": email, "nombre": (ag and ag["nombre"]) or nombre or email,
                          "agente_id": ag and ag["id"], "roles": roles}
    return session["usuario"]


@app.get("/auth/sso")
def auth_sso():
    """Vuelta del login en broker.pulppo.com: canjea el key de un solo uso por el agente (como el Coach IA)."""
    import requests
    key = request.args.get("key", "")
    if not key:
        return redirect("/?error=sso")
    try:
        r = requests.post(f"{PULPPO_API_URL}/login/magic-link/exchange", json={"key": key}, timeout=15)
        email = (r.json().get("agent") or {}).get("email", "") if r.ok else ""
    except (requests.RequestException, ValueError):
        email = ""
    if not email or not _entrar(email):
        return redirect("/?error=sso")
    return redirect("/")


@app.get("/auth/local")
def auth_local():
    if EN_VERCEL:
        abort(404)
    if not _entrar(request.args.get("email", "")):
        return "Ese correo no es asesor ni admin", 404
    tab, p = request.args.get("tab", ""), request.args.get("p", "")
    qs = "&".join(f"{k}={v}" for k, v in (("p", p), ("tab", tab)) if v.isalpha())
    return redirect(f"/?{qs}" if qs else "/")


@app.post("/auth/salir")
def salir():
    session.clear()
    return jsonify(ok=True)


# ---------------------------------------------------------------- páginas y archivos estáticos

@app.get("/")
def inicio():
    html = (HERE / "static" / "index.html").read_text()
    conf = {"brokerSsoUrl": BROKER_SSO_URL, "local": not EN_VERCEL}
    html = html.replace("<!--CONFIG-->", f"<script>window.CONFIG = {json.dumps(conf)};</script>")
    r = Response(html, mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/static/<path:p>")
def estaticos(p):
    r = send_from_directory(HERE / "static", p)
    r.headers["Cache-Control"] = "no-cache"  # al refrescar siempre toma la versión más reciente
    return r


@app.get("/ds/<path:p>")
def design_system(p):
    if not (p.startswith("fonts/") or p.startswith("assets/pulppo-") or p == "colors_and_type.css"):
        abort(404)
    return send_from_directory(DS, p)


@app.after_request
def cabeceras(r):
    r.headers["X-Robots-Tag"] = "noindex, nofollow"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["X-Content-Type-Options"] = "nosniff"
    if request.path.startswith(("/api/", "/archivo/")):
        r.headers["Cache-Control"] = "private, no-store"
    return r


@app.errorhandler(ErrorFlujo)
def error_flujo(e):
    return jsonify(error=str(e)), 400


# ---------------------------------------------------------------- API del asesor

@app.get("/api/yo")
@requiere()
def api_yo():
    u = usuario()
    return jsonify({**u, "admin": bool(u["roles"]), "finanzas": "finanzas" in u["roles"], "hoy": hoy().isoformat(),
                    "programas": programas(u), "bono_broker": bool(u.get("agente_id") and es_broker_bono(u["email"]))})


@app.get("/api/inicio")
@requiere()
def api_inicio():
    """Resumen para las tarjetas del inicio (cada una se calcula aparte: si una falla, la otra se muestra)."""
    u, out = usuario(), {}
    ps = programas(u)
    if "referidos" in ps:
        try:
            if u["roles"]:
                xs = [s for s in almacen.solicitudes() if s["tipo"] != "bono" and _puede_ver(u, s)]
                out["referidos"] = {"por_revisar": sum(calculo.estado_solicitud(s, hoy(), CFG) == "facturado" for s in xs)}
            if u.get("agente_id"):
                ag = agente_por_id(u["agente_id"])
                mias = [l for l in lineas_programa() if l["referidor_id"] == ag["id"]]
                out.setdefault("referidos", {})["totales"] = calculo.resumen(mias)
        except Exception as e:  # noqa: BLE001
            out["referidos"] = {"error": str(e)[:200]}
    if "bono" in ps:
        try:
            L = lineas_bono()
            if u["roles"]:
                xs = [s for s in almacen.solicitudes(tipo="bono") if _puede_ver(u, s)]
                out["bono"] = {"por_revisar": sum(s["estado"] == "facturado" for s in xs)}
            if u.get("agente_id") and es_broker_bono(u["email"]):
                out.setdefault("bono", {})["totales"] = bono.resumen([l for l in L if l["broker_id"] == u["agente_id"]])
        except Exception as e:  # noqa: BLE001
            out["bono"] = {"error": str(e)[:200]}
    for p in ps:  # solo el número de avisos nuevos: cada programa los muestra en su propia pantalla
        out.setdefault(p, {})["avisos_nuevos"] = sum(not a["leido"] for a in almacen.avisos(u["email"], p))
    return jsonify(out)


def _lineas_publicas(xs):
    campos = ("id", "referido", "referido_inmobiliaria", "nivel", "mes", "base", "porcentaje", "base_elegible", "monto",
              "monto_sin_reglas", "estado", "grupo", "motivo", "cobro_hasta", "regalia", "solicitud_id", "nivel_referidor", "periodo_tope")
    return [{k: x.get(k) for k in campos} for x in xs]


def _sol_publica(s):
    return {k: s.get(k) for k in ("id", "tipo", "periodo", "monto", "estado", "motivo_rechazo", "limite_correccion",
                                   "creada", "actualizada", "aprobada", "pago_desde", "pago_hasta", "pagada",
                                   "pago_referencia")} | {"pago_estimado": almacen.pago_estimado(s, CFG), "pago_estimado_fijada": bool(s.get("pago_estimado")), "cfdi": {k: s["cfdi"].get(k) for k in ("UUID", "Total", "SubTotal", "Folio", "Serie")},
                                                          "lineas": len(s["lineas"]), "tiene_comprobante": bool(s.get("comprobante"))}


def _anterior_de(ag):
    a = (anterior().get(ag.get("uid")) if ag.get("uid") else None) or {}
    pagos = [r for r in cxp() if r.get("email") == ag["email"]]
    sol = almacen.solicitudes(ag["id"], "anterior")  # la más reciente primero
    cobrado_cxp = round(sum(r["total"] for r in pagos if r["status"].lower() == "pagado"), 2)
    por_pagar_cxp = round(sum(r["total"] for r in pagos if r["status"].lower() != "pagado"), 2)
    limite = date.fromisoformat(CFG["programa_anterior_limite"])
    return {"disponible": a.get("disponible", 0), "excluido_mb": a.get("excluido_mb", 0),
            "en_proceso_plataforma": a.get("en_proceso", 0), "cobrado": cobrado_cxp, "por_pagar_cxp": por_pagar_cxp,
            "pagos": pagos, "entradas": a.get("entradas", []), "limite": limite.isoformat(),
            "abierto": hoy() <= limite, "solicitud": _sol_publica(sol[0]) if sol else None}


def resumen_referidor(ag):
    todas = lineas_programa()
    mias = [l for l in todas if l["referidor_id"] == ag["id"]]
    tot = calculo.resumen(mias)
    disp = [l for l in mias if l["estado"] == "disponible"]
    prox = [l for l in mias if l["estado"] == "proxima_ventana"]
    pend = [l for l in mias if l["estado"] == "pendiente_regalia"]
    sig = calculo.proxima_ventana(hoy(), CFG)
    vence = min((l["cobro_hasta"] for l in disp if l.get("cobro_hasta")), default=None)
    sols = almacen.solicitudes(ag["id"], "mensual")
    for s in sols:
        s["estado"] = calculo.estado_solicitud(s, hoy(), CFG)
    d = datos()
    referidos = calculo.mis_referidos(d, ag["id"])
    pct = {int(k): v for k, v in CFG["porcentaje_por_nivel"].items()}
    red = {}
    generado = {}
    for l in mias:
        generado[l["referido_id"]] = generado.get(l["referido_id"], 0) + l["monto"]
    for x in referidos:
        x["porcentaje"] = pct[x["nivel"]]
        x["beneficio"] = round(generado.get(x.pop("id"), 0), 2)
        red[x["nivel"]] = red.get(x["nivel"], 0) + 1
    c6 = calculo.comision_6m(d, ag["id"], mes_hoy()) if ag["id"] in d["agentes"] else 0
    return {
        "agente": {"id": ag["id"], "nombre": ag["nombre"], "email": ag["email"], "inmobiliaria": ag["company_nombre"],
                   "tipo": ag["tipo"], "afiliado": ag["afiliado"],
                   "activacion": ag["activacion"].isoformat() if ag["activacion"] else None,
                   "comision_6m": round(c6, 2), "nivel": calculo.nivel_plus(c6, CFG),
                   "excepcion": ag["email"] in almacen.excepciones()},
        "red": red, "referidos": referidos, "totales": tot,
        "por_facturar": {"monto": round(sum(l["monto"] for l in disp), 2), "lineas": len(disp), "vence": vence,
                         "vence_monto": round(sum(l["monto"] for l in disp if l.get("cobro_hasta") == vence), 2),
                         "meses": sorted({l["mes"] for l in disp}),
                         "pendiente_regalia": round(sum(l["monto"] for l in pend), 2),
                         "ventana_abierta": calculo.ventana_abierta(hoy(), CFG),
                         "ventana_cierra": calculo.dia(f"{hoy().year:04d}-{hoy().month:02d}", CFG["dia_limite_factura"]).isoformat(),
                         "proxima_abre": sig.isoformat(),
                         "proxima_cierra": calculo.dia(f"{sig.year:04d}-{sig.month:02d}", CFG["dia_limite_factura"]).isoformat(),
                         "proxima_monto": round(sum(l["monto"] for l in prox), 2),
                         "proxima_vence": min((l["cobro_hasta"] for l in prox if l.get("cobro_hasta")), default=None),
                         "rechazadas": [_sol_publica(s) for s in sols if s["estado"] == "rechazado"],
                         "en_revision": [_sol_publica(s) for s in sols if s["estado"] in ("facturado", "aprobado")]},
        "solicitudes": [_sol_publica(s) for s in sols],
        "lineas": _lineas_publicas(sorted(mias, key=lambda l: (l["mes"], l["referido"]), reverse=True)),
        "anterior": _anterior_de(ag),
        "inicio_programa": CFG["inicio_programa"],
        "facturar_a": CFG["datos_fiscales"] | {"impuestos": CFG["impuestos_factura"]},
        "reglas": {"umbral_profesional": CFG["umbral_profesional"], "umbral_elite": CFG["umbral_elite"],
                   "tope": CFG["tope_por_periodo"], "meses_para_cobrar": CFG["meses_para_cobrar"],
                   "dia_limite": CFG["dia_limite_factura"],
                   "pago_min": CFG["pago_dias_min"], "pago_max": CFG["pago_dias_max"]},
    }


def _referidor_objetivo():
    """El propio asesor, o el que pide un admin con ?como=<agente_id> (solo lectura)."""
    u = usuario()
    como = request.args.get("como")
    if como and u["roles"]:
        ag = agente_por_id(como)
        if not ag:
            abort(404)
        if not ve_inmo(u, ag["company_nombre"]):
            abort(403)
        return ag
    if not u.get("agente_id"):
        abort(404)
    return agente_por_id(u["agente_id"]) or abort(404)


@app.get("/api/resumen")
@requiere()
def api_resumen():
    ag = _referidor_objetivo()
    r = resumen_referidor(ag)
    r["solo_lectura"] = bool(request.args.get("como"))
    r["avisos"] = almacen.avisos(ag["email"], "referidos") if not r["solo_lectura"] else []
    return jsonify(r)


@app.post("/api/avisos/leidos")
@requiere()
def api_avisos_leidos():
    almacen.marcar_leidos(usuario()["email"], "bono" if request.args.get("programa") == "bono" else "referidos")
    return jsonify(ok=True)


def _archivo_subido(campo):
    f = request.files.get(campo)
    return (f.filename, f.read(), f.mimetype) if f and f.filename else None


def _quien_factura(u):
    """(agente, rol del evento): el propio asesor, o el que indica un admin con `como` (sube la factura en su nombre)."""
    como = request.form.get("como")
    if como:
        if not u["roles"]:
            abort(403)
        ag = agente_por_id(como) or abort(404)
        if not ve_inmo(u, ag["company_nombre"]):
            abort(403)
        return ag, "finanzas" if "finanzas" in u["roles"] else "kam"
    if not u.get("agente_id"):
        abort(403)
    return agente_por_id(u["agente_id"]), "referidor"


@app.post("/api/facturar")
@requiere()
def api_facturar():
    u = usuario()
    ag, rol = _quien_factura(u)
    tipo = request.form.get("tipo", "mensual")
    pdf, xml = _archivo_subido("pdf"), _archivo_subido("xml")
    ref = {"id": ag["id"], "email": ag["email"], "nombre": ag["nombre"], "inmobiliaria": ag["company_nombre"]}
    corregir = request.form.get("corregir") or None
    if tipo == "mensual":  # cada línea guarda sus operaciones (desglose congelado al facturar)
        estados = {"disponible"} if rol == "referidor" else {"disponible", "proxima_ventana"}  # el admin no espera a la ventana
        disp = [{"id": l["id"], "monto": l["monto"], "mes": l["mes"], "referido": l["referido"], "nivel": l["nivel"],
                 "referido_inmobiliaria": l["referido_inmobiliaria"], "porcentaje": l["porcentaje"], "base": l["base"],
                 "base_elegible": l["base_elegible"], "cobro_hasta": l["cobro_hasta"], "desglose": calculo.desglose(l)}
                for l in lineas_programa() if l["referidor_id"] == ag["id"] and l["estado"] in estados]
        s = almacen.facturar(CFG, hoy(), ref, "mensual", disp, pdf and pdf[:2], xml and xml[:2], u["email"], corregir, rol)
    elif tipo == "anterior":
        a = _anterior_de(ag)
        s = almacen.facturar(CFG, hoy(), ref, "anterior", [] if corregir else a["entradas"], pdf and pdf[:2], xml and xml[:2],
                             u["email"], corregir, rol)
    else:
        abort(400)
    return jsonify(_sol_publica(s))


@app.get("/archivo/<sid>/<cual>")
@requiere()
def archivo(sid, cual):
    s = almacen.solicitud(sid) or abort(404)
    u = usuario()
    if s["referidor_id"] != u.get("agente_id") and not _puede_ver(u, s):
        abort(403)
    campo = {"pdf": "factura_pdf", "xml": "factura_xml", "comprobante": "comprobante"}.get(cual) or abort(404)
    ref = s.get(campo) or abort(404)
    tipo = {"pdf": "application/pdf", "xml": "application/xml"}.get(cual, "application/octet-stream")
    nombre = f"{cual}-{s['referidor_nombre']}-{s['periodo']}".replace(" ", "_")
    ext = {"pdf": ".pdf", "xml": ".xml"}.get(cual, "")
    return Response(almacen.leer_archivo(ref), mimetype=tipo,
                    headers={"Content-Disposition": f'inline; filename="{nombre}{ext}"'})


# ---------------------------------------------------------------- API de admins (Finanzas y KAMs)

def _puede_ver(u, s):
    return bool(u.get("roles")) and ve_inmo(u, s.get("inmobiliaria"))


def _sol_admin(s):
    r = _sol_publica(s) | {"referidor_id": s["referidor_id"], "referidor_nombre": s["referidor_nombre"],
                           "referidor_email": s["referidor_email"], "inmobiliaria": s["inmobiliaria"]}
    if s["estado"] == "aprobado":  # días contra la fecha de pago aproximada (negativo = ya pasó)
        r["dias_para_pagar"] = (date.fromisoformat(r["pago_estimado"]) - hoy()).days
    return r


@app.get("/api/admin/solicitudes")
@requiere("finanzas", "kam")
def api_admin_solicitudes():
    u = usuario()
    bono_ = request.args.get("programa") == "bono"
    xs = [s for s in almacen.solicitudes() if (s["tipo"] == "bono") == bono_ and _puede_ver(u, s)]
    for s in xs:
        s["estado"] = calculo.estado_solicitud(s, hoy(), CFG)
    return jsonify([_sol_admin(s) for s in xs])


def _revision(s):
    """Validaciones de apoyo para quien aprueba. `motivo`: cómo se le explica al asesor si se rechaza (precarga el rechazo)."""
    c, checks = s["cfdi"], []
    sub = c.get("SubTotal")
    if sub is not None:
        ok = abs(sub - s["monto"]) <= 1
        checks.append({"ok": ok, "texto": f"Subtotal del XML {sub:,.2f} vs monto calculado {s['monto']:,.2f}" + ("" if ok else " (no coincide)"),
                       "motivo": f"El subtotal de la factura (${sub:,.2f}) no coincide con el monto a cobrar (${s['monto']:,.2f}). "
                                 f"Emítela por ${s['monto']:,.2f} más IVA."})
    df = CFG["datos_fiscales"]
    rfc = os.environ.get("RFC_PULPPO") or CFG.get("rfc_pulppo") or df["rfc"]
    checks.append({"ok": (c.get("ReceptorRfc") or "").upper() == rfc.upper(), "texto": f"Receptor {c.get('ReceptorRfc')} (debe ser {rfc})",
                   "motivo": f"El RFC del receptor debe ser {rfc}; la factura dice {c.get('ReceptorRfc') or 'sin RFC'}."})
    # Datos de CFDI 4.0 del receptor (los XML 3.3 no los traen: no se revisan)
    for campo, debe, txt, nombre in (("UsoCFDI", df["uso_cfdi"], df["uso_cfdi_txt"], "uso de CFDI"),
                                     ("RegimenReceptor", df["regimen"], df["regimen_txt"], "régimen fiscal del receptor"),
                                     ("CPReceptor", df["codigo_postal"], f"C.P. {df['codigo_postal']}", "código postal del receptor")):
        if c.get(campo):
            ok = c[campo].upper() == debe
            checks.append({"ok": ok, "texto": f"{nombre[0].upper()}{nombre[1:]}: {c[campo]}" + ("" if ok else f" (debe ser {txt})"),
                           "motivo": f"El {nombre} debe ser {txt}; la factura dice {c[campo]}."})
    checks.append({"ok": bool(c.get("UUID")), "texto": f"UUID {c.get('UUID') or 'sin timbre fiscal'}",
                   "motivo": "El XML no trae el timbre fiscal (UUID). Sube el XML timbrado por el SAT."})
    con = c.get("Concepto") or ""
    if s["tipo"] == "bono":
        import re
        pedido = bono.concepto(s["periodo"])
        ok = bool(re.search(r"bono\s*1\W?5\W?10", con, re.I)) and s["periodo"].lower() in con.lower()
        checks.append({"ok": ok, "texto": f"Concepto: {con or '—'}" + ("" if ok else f" (debe ser “{pedido}”)"),
                       "motivo": f"El concepto debe ser “{pedido}”; la factura dice “{con or 'sin concepto'}”."})
        for l in s["lineas"]:
            for a in l.get("avisos") or []:
                checks.append({"ok": False, "texto": a, "motivo": None})
    else:
        checks.append({"ok": "referidos" in con.lower(), "texto": f"Concepto: {con or '—'}",
                       "motivo": f"El concepto debe ser “Comisiones: programa referidos”; la factura dice “{con or 'sin concepto'}”."})
    ultimo = next((e for e in reversed(almacen.eventos(s["id"])) if e["a"] == "facturado"), None)
    if ultimo:
        f = datetime.fromisoformat(ultimo["fecha"]).astimezone(calculo.MX).date()
        plazo = almacen.limite_lineas(s, CFG)
        dentro = (plazo is None or f <= plazo) or (s.get("limite_correccion") and f <= date.fromisoformat(s["limite_correccion"]))
        checks.append({"ok": bool(dentro), "texto": f"Subida el {f:%d/%m/%Y}" + ("" if dentro else " (fuera de plazo)"),
                       "motivo": f"La factura se subió el {f:%d/%m/%Y}, fuera del plazo para cobrar."})
    return checks


@app.get("/api/admin/solicitud/<sid>")
@requiere("finanzas", "kam")
def api_admin_solicitud(sid):
    s = almacen.solicitud(sid) or abort(404)
    if not _puede_ver(usuario(), s):
        abort(403)
    s["estado"] = calculo.estado_solicitud(s, hoy(), CFG)
    return jsonify(_sol_admin(s) | {"detalle": _con_desglose(s), "cfdi": s["cfdi"], "eventos": almacen.eventos(sid),
                                    "revision": _revision(s)})


def _con_desglose(s):
    """Líneas de la solicitud con las operaciones que generaron cada beneficio. Las facturas anteriores a este cambio
    no guardaron el desglose: se toma del cálculo actual."""
    if s["tipo"] != "mensual":
        return s["lineas"]
    actuales = None
    out = []
    for l in s["lineas"]:
        if "desglose" not in l:
            if actuales is None:
                actuales = {x["id"]: x for x in lineas_programa()}
            x = actuales.get(l["id"])
            l = l | {"desglose": calculo.desglose(x | {"monto": l["monto"]}) if x else [],
                     "referido_inmobiliaria": l.get("referido_inmobiliaria") or (x or {}).get("referido_inmobiliaria"),
                     "porcentaje": l.get("porcentaje") or (x or {}).get("porcentaje"), "base": l.get("base") or (x or {}).get("base")}
        out.append(l)
    return out


_COLS_DESGLOSE = ["Mes", "Referido", "Inmobiliaria del referido", "Nivel", "ID de la operación", "Propiedad", "Dirección",
                  "Fecha de cobro", "Comisión cobrada", "Parte del referido", "Comisión del referido", "% referidos", "Beneficio"]


def _filas_desglose(s):
    filas = []
    for l in _con_desglose(s):
        for o in l.get("desglose") or []:
            filas.append([calculo.etiqueta_mes(l["mes"]), l.get("referido"), l.get("referido_inmobiliaria"), l.get("nivel"),
                          o["operacion"], o.get("propiedad"), o.get("direccion"), o.get("fecha"), o.get("pago"),
                          f"{round((o.get('fraccion') or 0) * 100)} %", o.get("comision_referido"),
                          f"{round((l.get('porcentaje') or 0) * 100)} %", o["beneficio"]])
    return filas


def _csv_desglose(s):
    import csv
    import io
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(_COLS_DESGLOSE)
    w.writerows(_filas_desglose(s))
    w.writerow(["Total"] + [""] * (len(_COLS_DESGLOSE) - 2) + [s["monto"]])
    return ("\ufeff" + b.getvalue()).encode("utf-8")  # con BOM para que Excel respete los acentos


def _nombre_desglose(s):
    return f"Desglose referidos - {s['referidor_nombre']} - {_periodo_sol(s)}.csv".replace("/", "-")


def _periodo_sol(s):
    meses = sorted({l["mes"] for l in s["lineas"] if l.get("mes")})
    return ", ".join(calculo.etiqueta_mes(m) for m in meses) or s["periodo"]


@app.get("/api/admin/solicitud/<sid>/desglose.csv")
@requiere("finanzas", "kam")
def api_desglose_csv(sid):
    s = almacen.solicitud(sid) or abort(404)
    if not _puede_ver(usuario(), s) or s["tipo"] != "mensual":
        abort(403 if s["tipo"] == "mensual" else 404)
    from urllib.parse import quote
    return Response(_csv_desglose(s), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(_nombre_desglose(s))}"})


@app.post("/api/admin/solicitud/<sid>/enviar-desglose")
@requiere("finanzas", "kam")
def api_enviar_desglose(sid):
    """Manda al bróker el desglose de su factura: tabla en el correo + CSV adjunto. Queda en el historial."""
    from html import escape
    s = almacen.solicitud(sid) or abort(404)
    u = usuario()
    if not _puede_ver(u, s) or s["tipo"] != "mensual":
        abort(403)
    nota = str((request.get_json(silent=True) or {}).get("nota", "")).strip()[:1000]
    filas = _filas_desglose(s)
    mx = lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else escape(str(v or ""))  # noqa: E731
    th = "".join(f'<th style="text-align:left;padding:4px 8px;border-bottom:1px solid #212322;font-size:12px">{escape(c)}</th>'
                 for c in ("Mes", "Referido", "ID de la operación", "Dirección", "Comisión del referido", "Beneficio"))
    tr = "".join("<tr>" + "".join(f'<td style="padding:4px 8px;border-bottom:1px solid #F3F3F3;font-size:12px">{c}</td>' for c in (
        escape(f[0]), escape(f[1] or ""), escape(f[4] or ""), escape(f[6] or ""), mx(f[10]), mx(f[12]))) + "</tr>" for f in filas)
    html = (f'<div style="font-family:Nunito Sans,Arial,sans-serif;color:#212322">'
            f'<h2 style="font-family:Georgia,serif;font-weight:400">Desglose de tu beneficio de referidos</h2>'
            f'<p>Hola {escape(s["referidor_nombre"])}, este es el detalle de las operaciones que generaron tu beneficio de '
            f'{escape(_periodo_sol(s))} por <b>{mx(s["monto"])}</b> (más IVA).</p>'
            + (f'<p style="white-space:pre-line">{escape(nota)}</p>' if nota else "")
            + f'<table style="border-collapse:collapse">{th}{tr}</table>'
            f'<p style="font-size:12px">El beneficio de cada operación es el porcentaje de tu nivel sobre la comisión que cobró tu referido. '
            f'Va adjunto en Excel (CSV).</p>'
            f'<p style="color:#B7B7B7;font-size:12px">Programa de referidos · Pulppo</p></div>')
    almacen.enviar_correo(s["referidor_email"], f"Desglose de tu beneficio de referidos · {_periodo_sol(s)}", html,
                          [(_nombre_desglose(s), _csv_desglose(s))], estricto=True)
    almacen.registrar_evento(sid, u["email"], "finanzas" if "finanzas" in u["roles"] else "kam",
                             f"Desglose enviado por correo a {s['referidor_email']}")
    return jsonify(ok=True, para=s["referidor_email"])


def _enlace():
    return request.host_url.rstrip("/") + "/"


@app.post("/api/admin/solicitud/<sid>/aprobar")
@requiere("finanzas", "kam")
def api_aprobar(sid):
    s = almacen.solicitud(sid) or abort(404)
    if not _puede_ver(usuario(), s):
        abort(403)
    u = usuario()
    return jsonify(_sol_admin(almacen.aprobar(CFG, hoy(), sid, u["email"], "finanzas" if "finanzas" in u["roles"] else "kam", _enlace())))


@app.post("/api/admin/solicitud/<sid>/rechazar")
@requiere("finanzas", "kam")
def api_rechazar(sid):
    s = almacen.solicitud(sid) or abort(404)
    if not _puede_ver(usuario(), s):
        abort(403)
    u = usuario()
    motivo = (request.get_json(silent=True) or {}).get("motivo", "")
    return jsonify(_sol_admin(almacen.rechazar(CFG, hoy(), sid, u["email"], "finanzas" if "finanzas" in u["roles"] else "kam",
                                               motivo, _enlace())))


@app.post("/api/admin/solicitud/<sid>/pagar")
@requiere("finanzas")
def api_pagar(sid):
    s = almacen.marcar_pagado(sid, usuario()["email"], request.form.get("fecha"), request.form.get("referencia"),
                              _archivo_subido("comprobante"), _enlace())
    return jsonify(_sol_admin(s))


@app.post("/api/admin/solicitud/<sid>/fecha-pago")
@requiere("finanzas")
def api_fecha_pago(sid):
    s = almacen.solicitud(sid) or abort(404)
    fecha = (request.get_json(silent=True) or {}).get("fecha") or None
    return jsonify(_sol_admin(almacen.fecha_pago_estimada(sid, usuario()["email"], fecha)))


@app.post("/api/admin/solicitud/<sid>/revertir")
@requiere("finanzas")
def api_revertir(sid):
    return jsonify(_sol_admin(almacen.regresar_a_revision(sid, usuario()["email"])))


# ---------------------------------------------------------------- Bono 1·5·10

_CAMPOS_BONO = ("id", "operacion", "estado_op", "lado", "cierre", "propiedad", "colonia", "valor", "comision", "fraccion", "base",
                "porcentaje", "monto", "comision_propiedad", "exclusiva_meses", "estado", "grupo", "motivo", "cobrable_desde",
                "solicitud_id", "avisos")


def _broker_bono():
    """El propio bróker, o el que pide un admin con ?como=<agente_id> (solo lectura)."""
    u = usuario()
    como = request.args.get("como")
    if como and u["roles"]:
        return como, True
    if not u.get("agente_id") or not es_broker_bono(u["email"]):
        abort(403)
    return u["agente_id"], False


@app.get("/api/bono/resumen")
@requiere()
def api_bono_resumen():
    aid, ro = _broker_bono()
    mias = [l for l in lineas_bono() if l["broker_id"] == aid]
    if ro and mias and not ve_inmo(usuario(), mias[0]["inmobiliaria"]):
        abort(403)
    sols = almacen.solicitudes(aid, "bono")
    for s in sols:
        s["estado"] = calculo.estado_solicitud(s, hoy(), CFG)
    from bson import ObjectId
    ag = db().agents.find_one({"_id": ObjectId(aid)}, {"firstName": 1, "lastName": 1, "company.name": 1, "type": 1}) or {}
    ag = {"nombre": f"{ag.get('firstName') or ''} {ag.get('lastName') or ''}".strip(), "company_nombre": (ag.get("company") or {}).get("name"),
          "tipo": ag.get("type")}
    visibles = [l for l in mias if l["estado"] != "excluido"]
    return jsonify({
        "agente": {"id": aid, "nombre": ag.get("nombre") or usuario()["nombre"], "inmobiliaria": ag.get("company_nombre"),
                   "tipo": ag.get("tipo")},
        "totales": bono.resumen(mias), "solo_lectura": ro,
        "operaciones": [{k: l.get(k) for k in _CAMPOS_BONO} | {"concepto": bono.concepto(l["operacion"])} for l in visibles],
        "no_aplica": [{k: l.get(k) for k in ("operacion", "cierre", "propiedad", "motivo")} for l in mias if l["estado"] == "excluido"],
        "solicitudes": [_sol_publica(s) for s in sols],
        "avisos": [] if ro else almacen.avisos(usuario()["email"], "bono"),
        "facturar_a": CFG["datos_fiscales"] | {"impuestos": CFG["impuestos_factura"]},
        "reglas": {"desde": CFG["bono_desde"], "porcentaje": CFG["bono_porcentaje"], "comision_min": CFG["bono_comision_min"],
                   "meses_exclusiva": CFG["bono_meses_exclusiva"], "pago_min": CFG["pago_dias_min"], "pago_max": CFG["pago_dias_max"]},
    })


@app.post("/api/bono/facturar")
@requiere()
def api_bono_facturar():
    u = usuario()
    if request.form.get("como"):  # un admin sube la factura en nombre del bróker
        ag, rol = _quien_factura(u)
    elif u.get("agente_id") and es_broker_bono(u["email"]):
        ag, rol = agente_por_email(u["email"]), "referidor"
    else:
        abort(403)
    oid, corregir = request.form.get("operacion", ""), request.form.get("corregir") or None
    lineas = []
    if not corregir:
        l = next((l for l in lineas_bono() if l["id"] == oid and l["broker_id"] == ag["id"]), None)
        if not l or l["estado"] != "disponible":
            raise ErrorFlujo("Esta operación no tiene bono disponible para facturar.")
        lineas = [{k: l[k] for k in ("id", "operacion", "propiedad", "cierre", "comision", "fraccion", "base", "porcentaje",
                                     "monto", "avisos")}]
    ref = {"id": ag["id"], "email": ag["email"], "nombre": ag["nombre"], "inmobiliaria": ag["company_nombre"]}
    pdf, xml = _archivo_subido("pdf"), _archivo_subido("xml")
    s = almacen.facturar(CFG, hoy(), ref, "bono", lineas, pdf and pdf[:2], xml and xml[:2], u["email"], corregir, rol)
    return jsonify(_sol_publica(s))


@app.get("/api/admin/bono/operaciones")
@requiere("finanzas", "kam")
def api_admin_bono_operaciones():
    u = usuario()
    xs = [l for l in lineas_bono() if not l["inmobiliaria"] or ve_inmo(u, l["inmobiliaria"])]
    campos = _CAMPOS_BONO + ("broker_id", "broker", "broker_email", "broker_tipo", "inmobiliaria")
    return jsonify({"operaciones": [{k: l.get(k) for k in campos} for l in xs], "desde": CFG["bono_desde"]})


@app.get("/api/admin/referidores")
@requiere("finanzas", "kam")
def api_admin_referidores():
    u = usuario()
    d = datos()
    lineas = lineas_programa()
    por = {}
    for l in lineas:
        t = por.setdefault(l["referidor_id"], {})
        t[l["grupo"]] = t.get(l["grupo"], 0) + l["monto"]
    ant = anterior()
    kam_inmos = None if "finanzas" in u["roles"] else inmos_del_kam(u["email"])
    out = []
    ids = {r["referidor"] for r in d["red"]}
    for aid in ids:
        a = d["agentes"].get(aid)
        if not a or not a["activo"] or not calculo.participa(a, d["companias"]):
            continue
        if kam_inmos is not None and calculo.norm_company(a["company_nombre"]) not in kam_inmos:
            continue
        an = ant.get(a.get("uid")) or {}
        t = por.get(aid, {})
        n = len(calculo.mis_referidos(d, aid))
        out.append({"id": aid, "nombre": a["nombre"], "email": a["email"], "inmobiliaria": a["company_nombre"], "tipo": a["tipo"],
                    "referidos": n, "a_cobrar": round(t.get("a_cobrar", 0), 2), "en_proceso": round(t.get("en_proceso", 0), 2),
                    "cobrado": round(t.get("cobrado", 0), 2), "a_futuro": round(t.get("a_futuro", 0), 2),
                    "anterior_disponible": an.get("disponible", 0), "anterior_excluido_mb": an.get("excluido_mb", 0)})
    out.sort(key=lambda r: (-(r["a_cobrar"] + r["a_futuro"] + r["anterior_disponible"]), r["nombre"]))
    return jsonify(out)


@app.get("/api/admin/beneficios")
@requiere("finanzas", "kam")
def api_admin_beneficios():
    """Desglose de todos los beneficios generados por referidor, con estado y motivo (incluye lo no cobrable),
    y los cierres de regalías que tienen beneficios detenidos (doble check: los admins marcan el cierre como pagado)."""
    u = usuario()
    d = datos()
    kam_inmos = None if "finanzas" in u["roles"] else inmos_del_kam(u["email"])
    filas, bloqueos = [], {}
    for l in lineas_programa():
        R = d["agentes"].get(l["referidor_id"])
        if kam_inmos is not None and calculo.norm_company(R["company_nombre"]) not in kam_inmos:
            continue
        f = {k: l.get(k) for k in ("referido", "referido_inmobiliaria", "nivel", "mes", "base", "porcentaje", "monto",
                                   "monto_sin_reglas", "estado", "grupo", "motivo", "cobro_hasta", "regalia", "nivel_referidor")}
        f |= {"referidor_id": R["id"], "referidor": R["nombre"], "referidor_inmobiliaria": R["company_nombre"],
              "referidor_tipo": R["tipo"], "por_regalia": l["estado"] == "pendiente_regalia" or "regalía" in (l["motivo"] or "")}
        filas.append(f)
        if f["por_regalia"]:
            k = (l.get("regalia_inmobiliaria") or l["referido_inmobiliaria"], l["mes"])
            b = bloqueos.setdefault(k, {"inmobiliaria": k[0], "mes": k[1], "regalia": l["regalia"], "cobro_hasta": l["cobro_hasta"],
                                        "referidores": set(), "pendiente": 0.0, "perdido": 0.0})
            b["referidores"].add(R["nombre"])
            if l["estado"] == "pendiente_regalia":
                b["pendiente"] += l["monto"]
            else:
                b["perdido"] += l["monto_sin_reglas"]
    bl = [{**b, "referidores": sorted(b["referidores"]), "pendiente": round(b["pendiente"], 2), "perdido": round(b["perdido"], 2)}
          for b in bloqueos.values()]
    bl.sort(key=lambda b: (b["perdido"] > 0, b["mes"], -b["pendiente"]))
    return jsonify({"lineas": filas, "bloqueos": bl, "meses_para_cobrar": CFG["meses_para_cobrar"]})


@app.get("/api/admin/simulacion")
@requiere("finanzas", "kam")
def api_simulacion():
    """Qué habría pasado con las reglas nuevas en meses anteriores (sin ventanas de factura)."""
    desde = request.args.get("desde", "2026-01")
    if len(desde) != 7 or desde > mes_hoy():
        abort(400)
    d = datos(desde)
    L = calculo.calcular(d, CFG, hoy(), (), almacen.excepciones(), desde=desde, simulacion=True)
    meses = {}
    for l in L:
        m = meses.setdefault(l["mes"], {"mes": l["mes"], "sin_reglas": 0, "excluido_mb": 0, "estandar": 0, "modulo1": 0,
                                        "regalia": 0, "tope": 0, "pendiente": 0, "a_pagar": 0})
        m["sin_reglas"] += l["monto_sin_reglas"]
        mot = l["motivo"]
        if l["estado"] == "excluido" and "Master" in mot:
            m["excluido_mb"] += l["monto_sin_reglas"]
        elif "Estándar" in mot:
            m["estandar"] += l["monto_sin_reglas"]
        elif "Módulo 1" in mot:
            m["modulo1"] += l["monto_sin_reglas"]
        elif "regalía" in mot and l["estado"] == "perdido":
            m["regalia"] += l["monto_sin_reglas"]
        elif l["grupo"] == "a_futuro":
            m["pendiente"] += l["monto"]
        else:
            m["a_pagar"] += l["monto"]
            m["tope"] += l["monto_sin_reglas"] - l["monto"]
    filas = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()} for _, m in sorted(meses.items())]
    return jsonify({"desde": desde, "meses": filas, "nota": "La plataforma reparte la comisión distinto (50 % productor / 50 % comprador); "
                    "aquí se usa la regla 25/25/50 acordada."})


@app.get("/api/admin/excepciones")
@requiere("finanzas", "kam")
def api_excepciones():
    return jsonify(almacen.excepciones_detalle())


@app.post("/api/admin/excepciones")
@requiere("finanzas")
def api_excepciones_agregar():
    almacen.agregar_excepcion((request.get_json(silent=True) or {}).get("email", ""), usuario()["email"])
    return jsonify(almacen.excepciones_detalle())


@app.delete("/api/admin/excepciones/<email>")
@requiere("finanzas")
def api_excepciones_quitar(email):
    almacen.quitar_excepcion(email)
    return jsonify(almacen.excepciones_detalle())


@app.post("/api/admin/actualizar")
@requiere("finanzas", "kam")
def api_actualizar():
    _cache.clear()
    datos(fresco=True)
    return jsonify(ok=True)


@app.get("/api/cron/regalias")
def api_cron():
    """Cron diario de Vercel: registra el día en que cada regalía aparece pagada (Mongo no guarda esa fecha)."""
    if EN_VERCEL and request.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
        abort(401)
    datos(fresco=True)
    return jsonify(ok=True)


@app.get("/api/cron/diagnostico")
def api_diagnostico():
    """Qué variables hay y si Mongo responde (protegido con CRON_SECRET; nunca devuelve valores)."""
    if EN_VERCEL and request.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
        abort(401)
    out = {v: bool(os.environ.get(v)) for v in ("MONGODB_URI", "DATABASE_URL", "POSTGRES_URL", "BLOB_READ_WRITE_TOKEN",
                                                 "BROKER_SSO_URL", "RESEND_API_KEY", "SECRET_KEY", "CRON_SECRET")}
    uri = os.environ.get("MONGODB_URI", "")
    out["mongo_uri_empieza_bien"] = uri.startswith(("mongodb://", "mongodb+srv://"))
    import hashlib
    out["mongo_uri_huella"] = hashlib.sha256(uri.encode()).hexdigest()[:8]
    out["mongo_uri_espacios"] = uri != uri.strip() or " " in uri
    out["variables_blob_mongo"] = sorted(k for k in os.environ if "BLOB" in k.upper() or "MONGO" in k.upper())
    out["entorno"] = os.environ.get("VERCEL_ENV")
    t = time.time()
    try:
        from pulppo_db import get_client
        c = get_client()
        c.admin.command("ping")
        out["mongo"] = f"ok ({c['pulppo'].agents.estimated_document_count()} agentes)"
    except Exception as e:  # noqa: BLE001
        out["mongo"] = type(e).__name__ + ": " + str(e)[:300].replace(uri, "<MONGODB_URI>")
    out["segundos"] = round(time.time() - t, 1)
    return jsonify(out)

if __name__ == "__main__":
    # Local: se reinicia solo al cambiar el código; misma liga, misma sesión (SECRET_KEY en .secretos.json) y mismos datos
    app.run(host="127.0.0.1", port=8060, debug=False, use_reloader=True,
            extra_files=[str(f) for f in (HERE / "static").glob("*")] + [str(HERE / "config.json")])
