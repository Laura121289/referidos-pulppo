"""Lo que se escribe: solicitudes de cobro, su historial, avisos, excepciones y regalías vistas como pagadas.

- Base: Postgres si hay DATABASE_URL/POSTGRES_URL (Vercel + Neon); si no, SQLite en datos-app/ (uso local).
- Archivos (factura PDF/XML, comprobante de pago): Vercel Blob si hay BLOB_READ_WRITE_TOKEN; si no, datos-app/archivos/.
  Nunca se exponen las URLs: la app los sirve después de revisar permisos.
- Correo: Resend si hay RESEND_API_KEY; si no, se escribe en datos-app/correos.log.

Flujo de una solicitud:  facturado → aprobado → pagado
                                   ↘ rechazado (con motivo) → facturado (corrección) …
"""
import json
import os
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import calculo

HERE = Path(__file__).resolve().parent
LOCAL = Path(os.environ["DATOS_APP"]) if os.environ.get("DATOS_APP") and not os.environ.get("VERCEL") else HERE / "datos-app"
PG_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
BLOB_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN")
BLOB_ACCESO = os.environ.get("BLOB_ACCESO", "private")  # los almacenes de Blob del proyecto son privados
RESEND_KEY = os.environ.get("RESEND_API_KEY")
REMITENTE = os.environ.get("CORREO_REMITENTE", "Referidos Pulppo <referidos@pulppo.com>")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS solicitudes (
  id TEXT PRIMARY KEY, tipo TEXT NOT NULL, referidor_id TEXT NOT NULL, referidor_email TEXT, referidor_nombre TEXT,
  inmobiliaria TEXT, periodo TEXT NOT NULL, monto DOUBLE PRECISION NOT NULL, lineas TEXT NOT NULL, estado TEXT NOT NULL,
  factura_pdf TEXT, factura_xml TEXT, cfdi TEXT, motivo_rechazo TEXT, limite_correccion TEXT,
  creada TEXT NOT NULL, actualizada TEXT NOT NULL, aprobada TEXT, pago_desde TEXT, pago_hasta TEXT,
  pagada TEXT, pago_referencia TEXT, comprobante TEXT, pago_estimado TEXT
);
CREATE TABLE IF NOT EXISTS eventos (
  id TEXT PRIMARY KEY, solicitud_id TEXT NOT NULL, fecha TEXT NOT NULL, actor_email TEXT, actor_rol TEXT,
  de TEXT, a TEXT NOT NULL, nota TEXT
);
CREATE TABLE IF NOT EXISTS avisos (
  id TEXT PRIMARY KEY, email TEXT NOT NULL, solicitud_id TEXT, texto TEXT NOT NULL, fecha TEXT NOT NULL, leido INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS excepciones (
  email TEXT PRIMARY KEY, agregada_por TEXT, fecha TEXT
);
CREATE TABLE IF NOT EXISTS deudas_vistas (
  company_id TEXT NOT NULL, mes TEXT NOT NULL, vista_en TEXT NOT NULL, saldada_en TEXT, PRIMARY KEY (company_id, mes)
);
CREATE TABLE IF NOT EXISTS regalias_vistas (
  company_id TEXT NOT NULL, mes TEXT NOT NULL, pagada_en TEXT NOT NULL, PRIMARY KEY (company_id, mes)
);
"""


class ErrorFlujo(Exception):
    """Transición no permitida o dato faltante: se muestra tal cual al usuario."""


# ---------------------------------------------------------------- conexión

def _conn():
    if PG_URL:
        import psycopg
        return psycopg.connect(PG_URL, autocommit=True)
    LOCAL.mkdir(exist_ok=True)
    c = sqlite3.connect(LOCAL / "referidos.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def _q(sql):
    return sql.replace("?", "%s") if PG_URL else sql


def ejecutar(sql, args=()):
    with _conn() as c:
        cur = c.cursor()
        cur.execute(_q(sql), args)
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def iniciar(cfg):
    with _conn() as c:
        for st in [s.strip() for s in ESQUEMA.split(";") if s.strip()]:
            c.cursor().execute(st)
    # Columnas agregadas después de crear la tabla (bases ya existentes)
    for col in ("pago_estimado",):
        try:
            ejecutar(f"ALTER TABLE solicitudes ADD COLUMN {col} TEXT")
        except Exception:  # noqa: BLE001 — ya existe
            pass
    if not ejecutar("SELECT email FROM excepciones LIMIT 1"):
        for e in cfg.get("excepciones_iniciales", []):
            agregar_excepcion(e, "config.json")


def ahora():
    n = datetime.now(timezone.utc)
    if os.environ.get("HOY") and not os.environ.get("VERCEL"):  # solo local: fecha simulada para probar plazos
        h = date.fromisoformat(os.environ["HOY"])
        n = n.astimezone(calculo.MX).replace(year=h.year, month=h.month, day=h.day).astimezone(timezone.utc)
    return n.isoformat(timespec="seconds")


# ---------------------------------------------------------------- archivos

def guardar_archivo(nombre, contenido, tipo):
    """Guarda y devuelve una referencia interna (URL de Blob o ruta local)."""
    ruta = f"referidos/{uuid.uuid4().hex}/{nombre}"
    if BLOB_TOKEN:
        import requests
        # Igual que @vercel/blob 2.x: el almacén es privado, así que hay que decirlo al subir (si no, responde 400).
        # Para leer basta el token (leer_archivo).
        partes = BLOB_TOKEN.split("_")
        r = requests.put("https://vercel.com/api/blob/", params={"pathname": ruta}, data=contenido, timeout=60,
                         headers={"authorization": f"Bearer {BLOB_TOKEN}", "x-api-version": "13",
                                  "x-vercel-blob-store-id": partes[3] if len(partes) > 3 else "",
                                  "x-vercel-blob-access": BLOB_ACCESO, "x-content-type": tipo,
                                  "x-add-random-suffix": "1", "x-content-length": str(len(contenido))})
        if not r.ok:
            raise RuntimeError(f"Vercel Blob respondió {r.status_code}: {r.text[:300]}")
        return r.json()["url"]
    f = LOCAL / "archivos" / ruta
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(contenido)
    return "local:" + ruta


def leer_archivo(ref):
    if ref.startswith("local:"):
        f = (LOCAL / "archivos" / ref[6:]).resolve()
        if LOCAL.resolve() not in f.parents:
            raise ErrorFlujo("Ruta inválida")
        return f.read_bytes()
    import requests
    r = requests.get(ref, timeout=60, headers={"authorization": f"Bearer {BLOB_TOKEN}"} if BLOB_TOKEN else {})
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------- CFDI

def leer_cfdi(xml_bytes):
    """Datos principales del XML de la factura (CFDI 3.3/4.0). Devuelve {} si no se puede leer."""
    try:
        raiz = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}
    loc = lambda t: t.rsplit("}", 1)[-1]  # noqa: E731
    if loc(raiz.tag) != "Comprobante":
        return {}
    out = {k: raiz.get(k) for k in ("Total", "SubTotal", "Moneda", "Fecha", "Serie", "Folio", "TipoDeComprobante")}
    for nodo in raiz.iter():
        t = loc(nodo.tag)
        if t == "Emisor":
            out["EmisorRfc"], out["EmisorNombre"] = nodo.get("Rfc"), nodo.get("Nombre")
        elif t == "Receptor":
            out["ReceptorRfc"], out["ReceptorNombre"] = nodo.get("Rfc"), nodo.get("Nombre")
            out["UsoCFDI"], out["RegimenReceptor"] = nodo.get("UsoCFDI"), nodo.get("RegimenFiscalReceptor")
            out["CPReceptor"] = nodo.get("DomicilioFiscalReceptor")
        elif t == "TimbreFiscalDigital":
            out["UUID"] = (nodo.get("UUID") or "").upper()
        elif t == "Concepto" and "Concepto" not in out:
            out["Concepto"] = nodo.get("Descripcion")
    for k in ("Total", "SubTotal"):
        try:
            out[k] = float(out[k])
        except (TypeError, ValueError):
            out[k] = None
    return out


# ---------------------------------------------------------------- correo y avisos

def avisar(email, texto, solicitud_id=None, asunto=None, html=None):
    ejecutar("INSERT INTO avisos (id, email, solicitud_id, texto, fecha) VALUES (?, ?, ?, ?, ?)",
             (uuid.uuid4().hex, email, solicitud_id, texto, ahora()))
    if asunto:
        enviar_correo(email, asunto, html or f"<p>{texto}</p>")


def enviar_correo(para, asunto, html, adjuntos=(), estricto=False):
    """adjuntos = [(nombre, bytes)]. estricto: si falla, se avisa a quien lo envió (en los avisos automáticos no)."""
    if not RESEND_KEY:
        LOCAL.mkdir(exist_ok=True)
        with open(LOCAL / "correos.log", "a") as f:
            f.write(json.dumps({"fecha": ahora(), "para": para, "asunto": asunto, "html": html,
                                "adjuntos": [n for n, _ in adjuntos]}, ensure_ascii=False) + "\n")
        return
    import base64
    import requests
    cuerpo = {"from": REMITENTE, "to": [para], "subject": asunto, "html": html}
    if adjuntos:
        cuerpo["attachments"] = [{"filename": n, "content": base64.b64encode(b).decode()} for n, b in adjuntos]
    try:
        requests.post("https://api.resend.com/emails", timeout=20, headers={"Authorization": f"Bearer {RESEND_KEY}"},
                      json=cuerpo).raise_for_status()
    except Exception as e:  # el aviso en la herramienta ya quedó; el correo no debe tumbar la operación
        print("Correo no enviado:", para, asunto, e)
        if estricto:
            raise ErrorFlujo("No se pudo enviar el correo. Intenta de nuevo o descarga el desglose y envíalo tú.")


def registrar_evento(sid, actor, rol, nota):
    """Evento informativo (sin cambio de estado), p. ej. 'Desglose enviado por correo'."""
    s = solicitud(sid)
    _evento(sid, actor, rol, s["estado"], s["estado"], nota)


def _de_programa(programa):
    """Avisos de un solo programa (los del bono son los de solicitudes tipo 'bono'; el resto es de referidos)."""
    return ("solicitud_id IN (SELECT id FROM solicitudes WHERE tipo = 'bono')" if programa == "bono" else
            "(solicitud_id IS NULL OR solicitud_id NOT IN (SELECT id FROM solicitudes WHERE tipo = 'bono'))")


def avisos(email, programa):
    return ejecutar(f"SELECT * FROM avisos WHERE email = ? AND {_de_programa(programa)} ORDER BY fecha DESC LIMIT 50", (email,))


def marcar_leidos(email, programa):
    ejecutar(f"UPDATE avisos SET leido = 1 WHERE email = ? AND {_de_programa(programa)}", (email,))


# ---------------------------------------------------------------- excepciones y regalías

def excepciones():
    return [r["email"] for r in ejecutar("SELECT email FROM excepciones ORDER BY email")]


def excepciones_detalle():
    return ejecutar("SELECT * FROM excepciones ORDER BY email")


def agregar_excepcion(email, por):
    email = email.strip().lower()
    if "@" not in email:
        raise ErrorFlujo("Correo inválido")
    if not ejecutar("SELECT email FROM excepciones WHERE email = ?", (email,)):
        ejecutar("INSERT INTO excepciones (email, agregada_por, fecha) VALUES (?, ?, ?)", (email, por, ahora()))


def quitar_excepcion(email):
    ejecutar("DELETE FROM excepciones WHERE email = ?", (email.strip().lower(),))


def registrar_regalias(regalias, hoy):
    """Guarda el primer día en que se vio pagada la regalía de cada inmobiliaria y mes; devuelve {(cid, mes): date}."""
    vistas = {(r["company_id"], r["mes"]): date.fromisoformat(r["pagada_en"]) for r in ejecutar("SELECT * FROM regalias_vistas")}
    for k, v in regalias.items():
        if v["estado"] == "paid" and k not in vistas:
            ejecutar("INSERT INTO regalias_vistas (company_id, mes, pagada_en) VALUES (?, ?, ?)", (k[0], k[1], hoy.isoformat()))
            vistas[k] = hoy
    return vistas


def registrar_deudas(vencidas, hoy):
    """Guarda cada regalía vencida que se ve y el día en que deja de estarlo; devuelve las ya saldadas
    [{company_id, mes, saldada_en: date}] (Mongo no guarda cuándo se pagó)."""
    hoy_txt = hoy.isoformat()
    actuales = {tuple(v) for v in vencidas}
    filas = {(r["company_id"], r["mes"]): r for r in ejecutar("SELECT * FROM deudas_vistas")}
    for k in actuales - set(filas):
        ejecutar("INSERT INTO deudas_vistas (company_id, mes, vista_en) VALUES (?, ?, ?)", (k[0], k[1], hoy_txt))
    saldadas = []
    for k, r in filas.items():
        fin = r["saldada_en"]
        if k in actuales and fin:  # volvió a vencer (no debería): sigue como adeudo abierto
            ejecutar("UPDATE deudas_vistas SET saldada_en = NULL WHERE company_id = ? AND mes = ?", k)
            continue
        if k not in actuales and not fin:
            ejecutar("UPDATE deudas_vistas SET saldada_en = ? WHERE company_id = ? AND mes = ?", (hoy_txt, k[0], k[1]))
            fin = hoy_txt
        if fin:
            saldadas.append({"company_id": k[0], "mes": k[1], "saldada_en": date.fromisoformat(fin)})
    return saldadas


# ---------------------------------------------------------------- solicitudes

def _sol(r):
    if r:
        r["lineas"] = json.loads(r["lineas"] or "[]")
        r["cfdi"] = json.loads(r["cfdi"] or "{}")
    return r


def solicitudes(referidor_id=None, tipo=None):
    sql, args = "SELECT * FROM solicitudes WHERE 1=1", []
    if referidor_id:
        sql += " AND referidor_id = ?"
        args.append(referidor_id)
    if tipo:
        sql += " AND tipo = ?"
        args.append(tipo)
    return [_sol(r) for r in ejecutar(sql + " ORDER BY creada DESC", args)]


def solicitud(sid):
    r = ejecutar("SELECT * FROM solicitudes WHERE id = ?", (sid,))
    return _sol(r[0]) if r else None


def eventos(sid):
    return ejecutar("SELECT * FROM eventos WHERE solicitud_id = ? ORDER BY fecha", (sid,))


def _evento(sid, actor, rol, de, a, nota=None):
    ejecutar("INSERT INTO eventos (id, solicitud_id, fecha, actor_email, actor_rol, de, a, nota) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
             (uuid.uuid4().hex, sid, ahora(), actor, rol, de, a, nota))


def _uuid_usado(uuid_cfdi, excepto):
    if not uuid_cfdi:
        return False
    for r in ejecutar("SELECT id, cfdi FROM solicitudes WHERE id <> ?", (excepto or "",)):
        if (json.loads(r["cfdi"] or "{}").get("UUID") or "") == uuid_cfdi:
            return True
    return False


def facturar(cfg, hoy, referidor, tipo, lineas, pdf, xml, actor, corregir=None, rol="referidor"):
    """El referidor sube su factura con todo lo disponible (nueva) o corrige una rechazada (corregir = id).
    Un admin (rol finanzas/kam) puede subirla en su nombre y fuera de la ventana del 1 al 10, pero lo vencido no revive
    (Laura, 27-oct-2026): queda en el historial con su correo y rol.
    referidor = {id, email, nombre, inmobiliaria}; lineas = [{id, monto, mes, cobro_hasta, ...}]; pdf/xml = (nombre, bytes).
    Programa nuevo: puede haber varias facturas en el mismo mes (lo que se libera después va en otra)."""
    previa = None
    if corregir:
        previa = solicitud(corregir)
        if not previa or previa["referidor_id"] != referidor["id"]:
            raise ErrorFlujo("No existe la factura a corregir.")
        if previa["estado"] != "rechazado":
            raise ErrorFlujo("Solo se corrige una factura rechazada.")
        if previa["limite_correccion"] and hoy > date.fromisoformat(previa["limite_correccion"]):
            raise ErrorFlujo("Se venció el plazo para corregir esta factura.")
    elif tipo == "anterior":
        t = calculo.tramo_abierto(cfg, hoy)
        if not t or any(calculo.tramo_anterior(l.get("mes"), cfg)["id"] != t["id"] for l in lineas):
            raise ErrorFlujo("El plazo para cobrar el programa anterior ya venció.")
        if any(x["estado"] != "rechazado" and tramo_de(x, cfg) == t["id"] for x in solicitudes(referidor["id"], "anterior")):
            raise ErrorFlujo("Ya enviaste la factura del programa anterior de este periodo.")
    elif tipo == "bono":  # una factura por operación
        ops = {l["id"] for l in lineas}
        if any(x["estado"] != "rechazado" and ops & {l["id"] for l in x["lineas"]} for x in solicitudes(referidor["id"], "bono")):
            raise ErrorFlujo("Ya enviaste la factura del bono de esta operación.")
    if not previa and tipo == "mensual" and hoy.day > cfg["dia_limite_factura"] and rol == "referidor":
        raise ErrorFlujo(f"Las facturas se suben del 1 al {cfg['dia_limite_factura']} de cada mes.")
    if not lineas and not previa:
        raise ErrorFlujo("No hay montos disponibles para facturar.")
    if not pdf or not pdf[1].startswith(b"%PDF"):
        raise ErrorFlujo("Sube la factura en PDF.")
    if not xml:
        raise ErrorFlujo("Sube el XML de la factura.")
    cfdi = leer_cfdi(xml[1])
    if not cfdi:
        raise ErrorFlujo("El XML no es un CFDI válido.")
    if _uuid_usado(cfdi.get("UUID"), previa and previa["id"]):
        raise ErrorFlujo("Esa factura (UUID) ya se usó en otra solicitud.")

    if previa:  # la corrección conserva lo ya solicitado y suma lo nuevo disponible
        ids = {l["id"] for l in previa["lineas"]}
        lineas = previa["lineas"] + [l for l in lineas if l["id"] not in ids]
    monto = round(sum(l["monto"] for l in lineas), 2)
    ref_pdf = guardar_archivo(pdf[0] or "factura.pdf", pdf[1], "application/pdf")
    ref_xml = guardar_archivo(xml[0] or "factura.xml", xml[1], "application/xml")
    t = ahora()
    if previa:
        ejecutar("""UPDATE solicitudes SET estado='facturado', lineas=?, monto=?, factura_pdf=?, factura_xml=?, cfdi=?,
                    actualizada=? WHERE id=?""",
                 (json.dumps(lineas), monto, ref_pdf, ref_xml, json.dumps(cfdi), t, previa["id"]))
        sid = previa["id"]
        _evento(sid, actor, rol, "rechazado", "facturado", "Factura corregida" + _en_nombre(referidor, rol))
    else:
        sid = uuid.uuid4().hex
        periodo = ("anterior" if tipo == "anterior" else lineas[0]["operacion"] if tipo == "bono"
                   else f"{hoy.year:04d}-{hoy.month:02d}")
        ejecutar("""INSERT INTO solicitudes (id, tipo, referidor_id, referidor_email, referidor_nombre, inmobiliaria, periodo,
                    monto, lineas, estado, factura_pdf, factura_xml, cfdi, creada, actualizada)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'facturado', ?, ?, ?, ?, ?)""",
                 (sid, tipo, referidor["id"], referidor["email"], referidor["nombre"], referidor.get("inmobiliaria"),
                  periodo, monto, json.dumps(lineas), ref_pdf, ref_xml, json.dumps(cfdi), t, t))
        _evento(sid, actor, rol, None, "facturado", _en_nombre(referidor, rol).lstrip(" ·") or None)
    return solicitud(sid)


def _en_nombre(referidor, rol):
    return "" if rol == "referidor" else f" · Subida por un admin en nombre de {referidor['nombre']}"


def tramo_de(s, cfg):
    """Tramo del programa anterior de una factura ("previo" o "diciembre"), según el mes de sus líneas."""
    return calculo.tramo_anterior(next((l.get("mes") for l in s.get("lineas") or [] if l.get("mes")), "2000-01"), cfg)["id"]


def limite_lineas(s, cfg):
    """Último día para cobrar lo incluido en la factura (el plazo más próximo de sus meses)."""
    if s["tipo"] == "anterior":
        return calculo.tramo_anterior(next((l.get("mes") for l in s["lineas"] if l.get("mes")), "2000-01"), cfg)["hasta"]
    if s["tipo"] == "bono":
        return None  # el bono no vence
    fechas = [date.fromisoformat(l["cobro_hasta"]) for l in s["lineas"] if l.get("cobro_hasta")]
    return min(fechas) if fechas else None


def _exigir(s, estado):
    if not s:
        raise ErrorFlujo("No existe la solicitud.")
    if s["estado"] != estado:
        raise ErrorFlujo(f"La solicitud está en '{s['estado']}', no en '{estado}'.")


def aprobar(cfg, hoy, sid, actor, rol, enlace=""):
    s = solicitud(sid)
    _exigir(s, "facturado")
    desde, hasta = hoy + timedelta(days=cfg["pago_dias_min"]), hoy + timedelta(days=cfg["pago_dias_max"])
    ejecutar("UPDATE solicitudes SET estado='aprobado', aprobada=?, pago_desde=?, pago_hasta=?, actualizada=? WHERE id=?",
             (hoy.isoformat(), desde.isoformat(), hasta.isoformat(), ahora(), sid))
    _evento(sid, actor, rol, "facturado", "aprobado")
    cuando = f"Fecha de pago aproximada: {date.fromisoformat(pago_estimado(s, cfg)):%d/%m/%Y}."
    avisar(s["referidor_email"], f"Tu factura de {_periodo_txt(s)} por {_mxn(s['monto'])} fue aprobada. {cuando}",
           sid, f"Tu factura {_programa(s)} fue aprobada",
           _html("Tu factura fue aprobada", [f"Periodo: {_periodo_txt(s)}", f"Monto: {_mxn(s['monto'])}", cuando], enlace, s))
    return solicitud(sid)


def rechazar(cfg, hoy, sid, actor, rol, motivo, enlace=""):
    motivo = (motivo or "").strip()
    if len(motivo) < 10:
        raise ErrorFlujo("Escribe el motivo del rechazo (mínimo 10 caracteres) para que el asesor sepa qué corregir.")
    s = solicitud(sid)
    _exigir(s, "facturado")
    plazo = limite_lineas(s, cfg)
    limite = None if s["tipo"] == "bono" else max(plazo or hoy, calculo.dias_habiles(hoy, cfg["dias_habiles_para_corregir"]))
    ejecutar("UPDATE solicitudes SET estado='rechazado', motivo_rechazo=?, limite_correccion=?, actualizada=? WHERE id=?",
             (motivo, limite and limite.isoformat(), ahora(), sid))
    _evento(sid, actor, rol, "facturado", "rechazado", motivo)
    plazo_txt = f"a más tardar el {limite:%d/%m/%Y}" if limite else "cuando la tengas"
    avisar(s["referidor_email"], f"Tu factura de {_periodo_txt(s)} fue rechazada.\nMotivo: {motivo}\n"
           f"Corrígela y vuelve a subirla {plazo_txt}.", sid, f"Corrige tu factura {_programa(s)}",
           _html("Tu factura fue rechazada", [f"Periodo: {_periodo_txt(s)}", f"Motivo: {motivo}",
                                              f"Sube la factura corregida {plazo_txt}."], enlace, s))
    return solicitud(sid)


def marcar_pagado(sid, actor, fecha_pago, referencia, comprobante=None, enlace=""):
    s = solicitud(sid)
    _exigir(s, "aprobado")
    if not fecha_pago:
        raise ErrorFlujo("Indica la fecha de pago.")
    try:
        f = date.fromisoformat(fecha_pago)
    except ValueError:
        raise ErrorFlujo("Fecha de pago inválida.")
    ref_c = guardar_archivo(comprobante[0] or "comprobante", comprobante[1], comprobante[2]) if comprobante else None
    ejecutar("UPDATE solicitudes SET estado='pagado', pagada=?, pago_referencia=?, comprobante=?, actualizada=? WHERE id=?",
             (f.isoformat(), (referencia or "").strip(), ref_c, ahora(), sid))
    _evento(sid, actor, "finanzas", "aprobado", "pagado", (referencia or "").strip() or None)
    avisar(s["referidor_email"], f"Pagamos tu factura de {_periodo_txt(s)} por {_mxn(s['monto'])} el {f:%d/%m/%Y}.",
           sid, "Pagamos tu bono 1·5·10" if s["tipo"] == "bono" else "Pagamos tu beneficio de referidos",
           _html("Tu bono fue pagado" if s["tipo"] == "bono" else "Tu beneficio fue pagado", [f"Periodo: {_periodo_txt(s)}", f"Monto: {_mxn(s['monto'])}",
                                             f"Fecha de pago: {f:%d/%m/%Y}"] + ([f"Referencia: {referencia}"] if referencia else []), enlace, s))
    return solicitud(sid)


def pago_estimado(s, cfg):
    """Fecha de pago aproximada: la que puso Finanzas o, por omisión, 45 días naturales (pago_dias_max) desde que se subió la factura."""
    if s.get("pago_estimado"):
        return s["pago_estimado"]
    subida = datetime.fromisoformat(s["creada"]).astimezone(calculo.MX).date()
    return (subida + timedelta(days=cfg["pago_dias_max"])).isoformat()


def fecha_pago_estimada(sid, actor, fecha):
    """Finanzas cambia la fecha de pago aproximada (vacía = regresa a la de omisión); el bróker la ve en su pantalla."""
    s = solicitud(sid)
    if not s:
        raise ErrorFlujo("No existe la solicitud.")
    if s["estado"] in ("pagado", "rechazado"):
        raise ErrorFlujo("Esta factura ya no admite fecha de pago aproximada.")
    if fecha:
        try:
            fecha = date.fromisoformat(fecha).isoformat()
        except ValueError:
            raise ErrorFlujo("Fecha inválida.")
    ejecutar("UPDATE solicitudes SET pago_estimado=?, actualizada=? WHERE id=?", (fecha or None, ahora(), sid))
    _evento(sid, actor, "finanzas", s["estado"], s["estado"],
            f"Fecha de pago aproximada: {date.fromisoformat(fecha):%d/%m/%Y}" if fecha else "Fecha de pago aproximada regresó a la de omisión")
    return solicitud(sid)


def regresar_a_revision(sid, actor):
    """Finanzas deshace una aprobación hecha por error (antes de pagar)."""
    s = solicitud(sid)
    _exigir(s, "aprobado")
    ejecutar("UPDATE solicitudes SET estado='facturado', aprobada=NULL, pago_desde=NULL, pago_hasta=NULL, actualizada=? WHERE id=?",
             (ahora(), sid))
    _evento(sid, actor, "finanzas", "aprobado", "facturado", "Aprobación revertida")
    return solicitud(sid)


def _programa(s):
    return "del bono 1·5·10" if s["tipo"] == "bono" else "de referidos"


def _periodo_txt(s):
    if s["tipo"] == "bono":
        return f"bono 1·5·10 de la operación {s['periodo']}"
    if s["tipo"] == "anterior":
        return "programa anterior"
    meses = sorted({l["mes"] for l in s["lineas"] if l.get("mes")})
    return "beneficios de " + ", ".join(calculo.etiqueta_mes(m) for m in meses) if meses else "beneficios"


def _mxn(v):
    return f"${v:,.2f}"


def _html(titulo, filas, enlace, s=None):
    from html import escape
    li = "".join(f'<li style="white-space:pre-line">{escape(f)}</li>' for f in filas)
    bono = bool(s and s["tipo"] == "bono")
    if enlace and bono:
        enlace += "?p=bono"
    btn = f'<p><a href="{escape(enlace)}">Ver en la herramienta de beneficios</a></p>' if enlace else ""
    return (f'<div style="font-family:Nunito Sans,Arial,sans-serif;color:#212322">'
            f'<h2 style="font-family:Georgia,serif;font-weight:400">{escape(titulo)}</h2><ul>{li}</ul>{btn}'
            f'<p style="color:#B7B7B7;font-size:12px">{"Bono 1·5·10" if bono else "Programa de referidos"} · Pulppo</p></div>')
