"""Motor de cálculo del programa de referidos relanzado (reglas de propuesta/definiciones.md, v2).

Dos partes:
- cargar(db, ...)  lee MongoDB (solo lectura) y devuelve datos planos.
- calcular(datos, cfg, hoy, solicitudes, excepciones)  aplica las reglas sin tocar Mongo (se prueba con datos de juguete).

Reglas implementadas
- Base: comisión cobrada por el referido en el mes = pagos (`operations.payments[]`, hora MX) de operaciones
  closed/paying, contada con 25/25/50: ambos lados vendedor 25 % + productor 25 % + comprador 50 %;
  lado vendedor 50/50 vendedor-productor; lado comprador 100 % comprador. Si falta el productor, su parte va al vendedor.
- Porcentaje por nivel de referido: 1 = 3 %, 2 = 2 %, 3 = 1 %. Tope de $600,000 de base por referido en cada periodo
  de 12 meses contados desde su activación (Módulo 1 de Academia). Solo consume tope lo que no se perdió.
- Referidor: debe tener el Módulo 1 y ser Profesional o Élite en el mes (comisión de los 6 meses cerrados anteriores,
  con 25/25/50; umbrales de config). Si es Estándar, ese mes se pierde. Excepciones: no dependen del nivel y acumulan.
- Master Broker: nunca cobra por referidos de su propia inmobiliaria (mismo _id o mismo nombre normalizado).
- Regalía: solo se cobra el mes si la inmobiliaria del referido pagó su regalía (`closes.status = paid`).
- Factura: siempre del día 1 al 10 de cada mes. Plazo de cobro (Laura, 15-oct-2026): lo generado en el mes M se puede
  facturar en las ventanas del 1 al 10 de los 2 meses siguientes (ej. octubre → 1–10 de noviembre o 1–10 de diciembre).
  Es un colchón por si la inmobiliaria del referido se atrasa con su regalía. Al terminar el plazo (día 10 de M+2):
  sin regalía pagada → no cobrable; pagada y sin factura → vencido.
"""
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

MX = timezone(timedelta(hours=-6))  # México sin horario de verano
HABI_RX = re.compile(r"tuhabi|habi\.co\b", re.I)
TEST_RX = re.compile(r"\btest\b|testing|\bdemo\b|prueba", re.I)
STOPWORDS = {"inmobiliaria", "inmobiliario", "real", "estate", "bienes", "raices", "luxury", "living", "properties"}
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]

# Estado de cada línea → grupo que ve el asesor
GRUPO = {
    "disponible": "a_cobrar", "proxima_ventana": "a_cobrar",
    "facturado": "en_proceso", "aprobado": "en_proceso", "rechazado": "en_proceso",
    "pagado": "cobrado",
    "en_curso": "a_futuro", "pendiente_regalia": "a_futuro",
    "perdido": "no_cobrable", "vencido": "no_cobrable", "excluido": "no_cobrable",
}


# ---------------------------------------------------------------- utilidades de fechas

def mes_de(dt):
    """datetime (UTC) → 'AAAA-MM' en hora de México."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt.astimezone(MX)
    return f"{d.year:04d}-{d.month:02d}"


def mes_mas(m, n):
    y, mm = int(m[:4]), int(m[5:7]) - 1 + n
    return f"{y + mm // 12:04d}-{mm % 12 + 1:02d}"


def meses_entre(a, b):
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def rango_meses(a, b):
    return [mes_mas(a, i) for i in range(meses_entre(a, b) + 1)] if a <= b else []


def dia(m, d):
    return date(int(m[:4]), int(m[5:7]), d)


def etiqueta_mes(m):
    return f"{MESES[int(m[5:7]) - 1]} {m[:4]}"


def norm_company(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.findall(r"[a-z0-9]+", s) if w not in STOPWORDS]
    return " ".join(words) or s.strip()


def norm_exacto(name):
    """Nombre en minúsculas, sin acentos ni signos (sin quitar palabras): para comparar contra la lista de excluidas."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", s))


def nivel_plus(comision_6m, cfg):
    if comision_6m >= cfg["umbral_elite"]:
        return "elite"
    if comision_6m >= cfg["umbral_profesional"]:
        return "profesional"
    return "estandar"


def dias_habiles(desde, n):
    d = desde
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


# ---------------------------------------------------------------- reparto 25/25/50

def reparto(op):
    """{agent_id(str): fracción} de la comisión de una operación según la regla 25/25/50."""
    g = lambda *ks: _get(op, ks)  # noqa: E731
    sb, bb = g("seller", "broker", "_id"), g("buyer", "broker", "_id")
    sp = g("seller", "producer", "_id") or g("property", "agent", "_id")
    side = op.get("side")
    out = defaultdict(float)
    if side == "buyer":
        partes = [(bb, 1.0)]
    elif side == "seller":
        partes = [(sb, .5), (sp or sb, .5)]
    else:  # both
        partes = [(sb, .25), (sp or sb, .25), (bb, .5)]
    for a, f in partes:
        if a:
            out[str(a)] += f
    return dict(out)


def direccion(prop):
    """Calle y número, colonia, alcaldía/municipio."""
    a = (prop or {}).get("address") or {}
    partes = [a.get("street"), _get(a, ("neighborhood", "name")), _get(a, ("city", "name"))]
    return ", ".join(p.strip() for p in partes if p and str(p).strip())


def desglose(linea):
    """Operaciones del referido que generaron el beneficio de una línea (referidor, referido, mes).
    El beneficio de cada operación es proporcional a lo que aportó a la base (respeta el tope)."""
    base = linea.get("base") or 0
    out = []
    for o in linea.get("ops") or []:
        out.append({"operacion": o.get("codigo") or o.get("op"), "propiedad": o.get("propiedad"), "direccion": o.get("direccion"),
                    "fecha": o.get("fecha"), "pago": o.get("pago"), "fraccion": o.get("fraccion"), "comision_referido": o.get("monto"),
                    "beneficio": round(linea["monto"] * (o.get("monto") or 0) / base, 2) if base else 0.0})
    return out


def _get(d, ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# ---------------------------------------------------------------- carga desde Mongo

def cargar(db, desde, hasta, excluidas=()):
    """Lee todo lo necesario para calcular los meses [desde, hasta] (AAAA-MM). Devuelve datos planos.
    excluidas: nombres de inmobiliarias que no participan (config.json → inmobiliarias_excluidas)."""
    from bson import ObjectId

    cfg_m1 = [c["_id"] for c in db.courses.find({"level": {"$lte": 1}, "isActive": True}, {"_id": 1})]

    # Red: cada asesor trae a todos sus referidores (nivel 1, 2 y 3)
    red, ids = {}, set()
    for a in db.agents.find({"referral.referrers.0": {"$exists": True}}, {"referral.referrers": 1}):
        for r in a["referral"]["referrers"]:
            if r.get("disabled") or not r.get("_id") or r.get("level") not in (1, 2, 3):
                continue
            k = (str(r["_id"]), str(a["_id"]))
            if k[0] == k[1]:
                continue
            prev = red.get(k)
            if not prev or r["level"] < prev[0]:  # hay duplicados: se queda el nivel más cercano
                f = r.get("referredAt")
                red[k] = (r["level"], f.astimezone(MX).date().isoformat() if f else None)
            ids.update(k)
    red = [{"referidor": k[0], "referido": k[1], "nivel": n, "desde": f} for k, (n, f) in red.items()]

    agentes, companias = {}, {}
    proy = {"firstName": 1, "lastName": 1, "email": 1, "type": 1, "status": 1, "deletedAt": 1, "company": 1,
            "courses": 1, "createdAt": 1, "level": 1, "uid": 1}
    for a in db.agents.find({"_id": {"$in": [ObjectId(i) for i in ids]}}, proy):
        agentes[str(a["_id"])] = _agente(a, cfg_m1)
    for c in db.companies.find({"_id": {"$in": list({ObjectId(a["company_id"]) for a in agentes.values() if a["company_id"]})}},
                               {"name": 1, "email": 1, "status": 1, "external": 1}):
        companias[str(c["_id"])] = _compania(c, excluidas)

    # Pagos desde 6 meses antes (para el nivel Pulppo Plus) hasta el mes final
    ini = datetime(int(mes_mas(desde, -6)[:4]), int(mes_mas(desde, -6)[5:7]), 1, 6, tzinfo=timezone.utc)
    fin_m = mes_mas(hasta, 1)
    fin = datetime(int(fin_m[:4]), int(fin_m[5:7]), 1, 6, tzinfo=timezone.utc)
    comisiones = defaultdict(lambda: {"total": 0.0, "ops": []})
    q = {"status.last": {"$in": ["closed", "paying"]}, "payments": {"$elemMatch": {"createdAt": {"$gte": ini, "$lt": fin}}}}
    p = {"id": 1, "side": 1, "status.last": 1, "payments.comission": 1, "payments.createdAt": 1, "comission.currency": 1,
         "seller.broker._id": 1, "seller.producer._id": 1, "buyer.broker._id": 1, "property.agent._id": 1,
         "property.company.email": 1, "property.company.name": 1, "property.internalId": 1,
         "property.address.street": 1, "property.address.neighborhood.name": 1, "property.address.city.name": 1}
    for op in db.operations.find(q, p).batch_size(5000):
        if _get(op, ("comission", "currency")) == "USD":
            continue  # sin tipo de cambio en Mongo (guía §7)
        rep = {a: f for a, f in reparto(op).items() if a in ids}
        if not rep:
            continue
        for pay in op.get("payments") or []:
            ts, val = pay.get("createdAt"), _get(pay, ("comission", "value")) or 0
            if not ts or not val or not (ini <= ts.astimezone(timezone.utc) < fin):
                continue
            m = mes_de(ts)
            for a, f in rep.items():
                c = comisiones[(a, m)]
                c["total"] += val * f
                c["ops"].append({"op": str(op["_id"]), "codigo": op.get("id"), "estado": _get(op, ("status", "last")),
                                 "pago": round(val, 2), "fraccion": f, "monto": round(val * f, 2),
                                 "fecha": ts.astimezone(MX).date().isoformat(),
                                 "propiedad": _get(op, ("property", "internalId")), "direccion": direccion(op.get("property"))})

    # Regalía mensual de cada inmobiliaria
    etiquetas = {etiqueta_mes(m): m for m in rango_meses(desde, hasta)}
    regalias = {}
    for c in db.closes.find({"id": {"$in": list(etiquetas)}}, {"id": 1, "company._id": 1, "status": 1, "updatedAt": 1}):
        cid = _get(c, ("company", "_id"))
        if cid:
            # Mongo no guarda cuándo se pagó: la app registra el primer día que la ve pagada (pagada_en)
            regalias[(str(cid), etiquetas[c["id"]])] = {"estado": c.get("status"), "pagada_en": None}

    return {"agentes": agentes, "companias": companias, "red": red, "comisiones": dict(comisiones),
            "regalias": regalias, "desde": desde, "hasta": hasta}


def _agente(a, cursos_m1):
    hechos = {c.get("_id"): c for c in a.get("courses") or [] if c.get("status") == "completed"}
    afiliado = bool(cursos_m1) and all(i in hechos for i in cursos_m1)
    activacion = None
    if afiliado:
        fechas = [hechos[i].get("finishedAt") for i in cursos_m1 if hechos[i].get("finishedAt")]
        f = max(fechas) if fechas else a.get("createdAt")
        activacion = f.astimezone(MX).date() if f else date(2000, 1, 1)
    comp = a.get("company") or {}
    return {"id": str(a["_id"]), "nombre": f"{(a.get('firstName') or '').strip()} {(a.get('lastName') or '').strip()}".strip(),
            "email": (a.get("email") or "").lower(), "tipo": a.get("type"), "activo": a.get("status") == "active" and not a.get("deletedAt"),
            "company_id": str(comp["_id"]) if comp.get("_id") else None, "company_nombre": comp.get("name") or "",
            "afiliado": afiliado, "activacion": activacion, "nivel_plataforma": a.get("level"), "uid": a.get("uid")}


def _compania(c, excluidas=()):
    """Inmobiliaria con el motivo por el que no participa (None si participa).
    Participan solo las activas de Pulppo con nombre; nunca Habi (por email), pruebas ni las de la lista de excluidas."""
    nombre = (c.get("name") or "").strip()
    motivo = None
    if not nombre:
        motivo = "sin nombre"
    elif c.get("status") == "inactive":
        motivo = "inactiva"
    elif c.get("external"):
        motivo = "externa"
    elif HABI_RX.search(c.get("email") or ""):
        motivo = "Habi"
    elif norm_exacto(nombre) in {norm_exacto(x) for x in excluidas}:
        motivo = "excluida"
    elif TEST_RX.search(nombre):
        motivo = "prueba"
    return {"nombre": nombre, "norm": norm_company(nombre), "excluida": bool(motivo), "motivo_exclusion": motivo}


def participa(agente, companias):
    """Usuario visible (con nombre) y de una inmobiliaria que participa."""
    if not agente or not agente.get("nombre") or not agente.get("company_id"):
        return False
    c = companias.get(agente["company_id"])
    return bool(c) and not c["excluida"]


# ---------------------------------------------------------------- cálculo

def cobro_hasta(m, cfg):
    """Último día para facturar lo generado en el mes m: día 10 (dia_limite_factura) del mes m + meses_para_cobrar."""
    return dia(mes_mas(m, cfg["meses_para_cobrar"]), cfg["dia_limite_factura"])


def ventana_abierta(hoy, cfg):
    """Las facturas se suben del día 1 al 10 de cada mes."""
    return hoy.day <= cfg["dia_limite_factura"]


def proxima_ventana(hoy, cfg):
    """Primer día de la siguiente ventana de factura (hoy si está abierta)."""
    if ventana_abierta(hoy, cfg):
        return hoy
    return dia(mes_mas(f"{hoy.year:04d}-{hoy.month:02d}", 1), 1)


def fecha_txt(d):
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def comision_6m(datos, agente, m):
    return sum(datos["comisiones"].get((agente, x), {}).get("total", 0) for x in rango_meses(mes_mas(m, -6), mes_mas(m, -1)))


def estado_solicitud(s, hoy, cfg):
    """Rechazada y sin corregir después del plazo → vencida."""
    if s["estado"] == "rechazado" and s.get("limite_correccion") and hoy > date.fromisoformat(s["limite_correccion"]):
        return "vencido"
    return s["estado"]


def calcular(datos, cfg, hoy, solicitudes=(), excepciones=(), desde=None, simulacion=False):
    """Devuelve {"lineas": [...], "referidores": {id: resumen}} para los meses desde..mes actual.

    solicitudes: [{id, referidor_id, periodo, estado, lineas: [{id, monto}], limite_correccion}] (tipo mensual)
    excepciones: emails de Master Brokers con excepción.
    simulacion: ignora ventanas de factura (todo mes cerrado con regalía pagada queda "disponible"). Para comparar
                las reglas nuevas contra meses pasados.
    """
    desde = desde or cfg["inicio_programa"]
    mes_hoy = f"{hoy.year:04d}-{hoy.month:02d}"
    hasta = min(datos["hasta"], mes_hoy)
    ag, co = datos["agentes"], datos["companias"]
    excep = {e.lower() for e in excepciones}
    pct = {int(k): v for k, v in cfg["porcentaje_por_nivel"].items()}

    en_solicitud = {}
    for s in solicitudes:
        st = estado_solicitud(s, hoy, cfg)
        for ln in s.get("lineas") or []:
            en_solicitud[ln["id"]] = (s, st, ln.get("monto"))

    lineas = []
    for rel in sorted(datos["red"], key=lambda r: (r["referidor"], r["referido"])):
        R, X = ag.get(rel["referidor"]), ag.get(rel["referido"])
        if not participa(R, co) or not participa(X, co):
            continue  # sin usuario o inmobiliaria visible, inactiva, Habi, prueba o excluida
        cx, cr = co[X["company_id"]], co[R["company_id"]]
        es_mb = R["tipo"] == "master"
        propia = es_mb and (R["company_id"] == X["company_id"] or (cr["norm"] and cr["norm"] == cx["norm"]))
        es_exc = R["email"] in excep
        consumido = defaultdict(float)  # por periodo de 12 meses

        for m in rango_meses(desde, hasta):
            base = datos["comisiones"].get((X["id"], m), {}).get("total", 0)
            if base <= 0:
                continue
            ln = {"id": f"{R['id']}|{X['id']}|{m}", "referidor_id": R["id"], "referido_id": X["id"],
                  "referido": X["nombre"], "referido_inmobiliaria": cx["nombre"], "nivel": rel["nivel"], "mes": m,
                  "base": round(base, 2), "porcentaje": pct[rel["nivel"]], "base_elegible": 0.0, "monto": 0.0,
                  "monto_sin_reglas": round(base * pct[rel["nivel"]], 2),
                  "estado": None, "motivo": "", "cobro_hasta": None, "regalia": None, "solicitud_id": None,
                  "ops": datos["comisiones"][(X["id"], m)]["ops"]}
            lineas.append(ln)
            fin_m = dia(mes_mas(m, 1), 1) - timedelta(days=1)
            nivel_r = nivel_plus(comision_6m(datos, R["id"], m), cfg)
            ln["nivel_referidor"] = nivel_r

            if propia:
                ln.update(estado="excluido", motivo="Master Broker: el referido es de su propia inmobiliaria")
                continue
            if not X["afiliado"] or X["activacion"] > fin_m:
                ln.update(estado="excluido", motivo="El referido no había completado el Módulo 1 de Academia")
                continue
            if not R["afiliado"] or R["activacion"] > fin_m:
                ln.update(estado="perdido", motivo="El referidor no había completado el Módulo 1 de Academia")
                continue
            if nivel_r == "estandar" and not es_exc:
                ln.update(estado="perdido", motivo="Nivel Estándar en el mes (se requiere Profesional o Élite)")
                continue

            # Estado del mes antes del tope (lo vencido o perdido no consume tope)
            reg = datos["regalias"].get((X["company_id"], m))
            ln["regalia"] = reg["estado"] if reg else "sin_cierre"
            limite = cobro_hasta(m, cfg)
            ln["cobro_hasta"] = None if es_exc else limite.isoformat()
            sol = en_solicitud.get(ln["id"])
            pagada = bool(reg) and reg["estado"] == "paid"
            if sol:
                s, st, monto_fijo = sol
                ln.update(estado=st, solicitud_id=s["id"])
                if st == "vencido":
                    ln["motivo"] = "La factura rechazada no se corrigió a tiempo"
            elif m >= mes_hoy:
                ln.update(estado="en_curso", motivo="Mes en curso: estimado")
            elif simulacion:
                if pagada:
                    ln["estado"] = "disponible"
                else:
                    ln.update(estado="perdido", motivo="La inmobiliaria del referido no ha pagado la regalía del mes")
                    continue
            elif hoy > limite and not es_exc:  # terminó el plazo de 2 meses (las excepciones acumulan)
                if pagada and (not reg.get("pagada_en") or reg["pagada_en"] <= limite):
                    ln.update(estado="vencido", motivo=f"No se facturó a tiempo (el plazo terminó el {fecha_txt(limite)})")
                else:
                    ln.update(estado="perdido", motivo=f"La inmobiliaria del referido no pagó la regalía del mes antes del {fecha_txt(limite)}")
                continue
            elif pagada:
                if ventana_abierta(hoy, cfg):
                    ln["estado"] = "disponible"
                else:
                    sig = proxima_ventana(hoy, cfg)
                    ln.update(estado="proxima_ventana",
                              motivo=f"Se factura del 1 al {cfg['dia_limite_factura']} de {MESES[sig.month - 1]}")
            else:
                que = "tiene su cierre del mes vencido" if ln["regalia"] == "expired" else "aún no paga la regalía del mes"
                hasta_txt = "" if es_exc else f"; si la paga a más tardar el {fecha_txt(limite)}, podrás cobrarlo"
                ln.update(estado="pendiente_regalia", motivo=f"La inmobiliaria del referido {que}{hasta_txt}")

            if ln["estado"] == "vencido":
                continue
            # Tope por periodo de 12 meses desde la activación del referido
            act_m = f"{X['activacion'].year:04d}-{X['activacion'].month:02d}"
            periodo = max(0, meses_entre(act_m, m)) // cfg["meses_por_periodo"]
            ln["periodo_tope"] = periodo + 1
            elegible = max(0.0, min(base, cfg["tope_por_periodo"] - consumido[periodo]))
            consumido[periodo] += elegible
            ln["base_elegible"] = round(elegible, 2)
            ln["monto"] = round(elegible * ln["porcentaje"], 2)
            if sol and sol[2] is not None:
                ln["monto"] = sol[2]  # lo facturado queda congelado
            if elegible <= 0:
                ln.update(estado="perdido", motivo="Se alcanzó el tope de $600,000 del periodo")

    for ln in lineas:
        ln["grupo"] = GRUPO[ln["estado"]]
    return lineas


def mis_referidos(datos, referidor_id):
    """Referidos (niveles 1–3) que pueden generarle beneficio al referidor con las reglas nuevas:
    usuario activo y visible, de una inmobiliaria que participa y, si el referidor es Master Broker, de otra inmobiliaria.
    Los improductivos (sin operaciones) aparecen: si cierran, generan. Los no afiliados (sin Módulo 1 de Academia)
    aparecen marcados (afiliado = False) para que el referidor lo impulse a completarlo."""
    ag, co = datos["agentes"], datos["companias"]
    R = ag.get(referidor_id)
    if not participa(R, co):
        return []
    out = []
    for rel in datos["red"]:
        if rel["referidor"] != referidor_id:
            continue
        X = ag.get(rel["referido"])
        if not participa(X, co) or not X["activo"]:
            continue
        cx, cr = co[X["company_id"]], co[R["company_id"]]
        if R["tipo"] == "master" and (R["company_id"] == X["company_id"] or (cr["norm"] and cr["norm"] == cx["norm"])):
            continue
        out.append({"id": X["id"], "nombre": X["nombre"], "inmobiliaria": cx["nombre"], "nivel": rel["nivel"],
                    "desde": rel.get("desde"), "afiliado": X["afiliado"], "porcentaje": None, "beneficio": 0.0})
    return sorted(out, key=lambda x: (x["nivel"], not x["afiliado"], x["nombre"].lower()))


def resumen(lineas):
    """Totales por grupo y por estado."""
    t = defaultdict(float)
    for ln in lineas:
        t[ln["grupo"]] += ln["monto"]
        t["estado:" + ln["estado"]] += ln["monto"]
    return {k: round(v, 2) for k, v in t.items()}


# ---------------------------------------------------------------- programa anterior (accountingentries)

def programa_anterior(db, excluidas=()):
    """Saldos del programa anterior por referidor (uid). No se recalculan: solo se quita lo que el Master Broker
    generó por referidos de su propia inmobiliaria. Mismo filtro de inmobiliarias que el programa nuevo
    (referidor y referido con usuario e inmobiliaria activa, sin Habi, pruebas ni excluidas).

    Devuelve {uid: {"email", "nombre", "disponible", "excluido_mb", "en_proceso", "cobrado", "potencial", "entradas": [...]}}
    """
    from bson import ObjectId
    out = defaultdict(lambda: {"disponible": 0.0, "excluido_mb": 0.0, "en_proceso": 0.0, "cobrado": 0.0, "entradas": []})
    entradas = list(db.accountingentries.find({}, {"referrer.uid": 1, "referrer.email": 1, "referrer.firstName": 1,
                                                    "referrer.lastName": 1, "referrer.level": 1, "referrer.company": 1,
                                                    "agent._id": 1, "agent.firstName": 1, "agent.lastName": 1,
                                                    "agent.company": 1, "status": 1, "value": 1, "credit": 1,
                                                    "debit": 1, "close": 1, "operation": 1, "operationId": 1}))
    uids = {_get(e, ("referrer", "uid")) for e in entradas} - {None}
    tipos = {a["uid"]: a for a in db.agents.find({"uid": {"$in": list(uids)}, "deletedAt": None},
                                                  {"uid": 1, "type": 1, "company": 1, "email": 1, "firstName": 1, "lastName": 1})}
    cids = {_get(a, ("company", "_id")) for a in tipos.values()} | {_get(e, ("agent", "company", "_id")) for e in entradas}
    comps = {c["_id"]: _compania(c, excluidas) for c in db.companies.find({"_id": {"$in": list(cids - {None})}},
                                                                         {"name": 1, "email": 1, "status": 1, "external": 1})}
    ok = lambda cid: cid in comps and not comps[cid]["excluida"]  # noqa: E731
    for e in entradas:
        uid = _get(e, ("referrer", "uid"))
        ra = tipos.get(uid)
        if not ra or not (ra.get("firstName") or ra.get("lastName")) or not ok(_get(ra, ("company", "_id"))):
            continue  # referidor sin usuario o de inmobiliaria que no participa
        if not e.get("credit") and (not _get(e, ("agent", "_id")) or not ok(_get(e, ("agent", "company", "_id")))):
            continue  # referido sin usuario o de inmobiliaria que no participa
        r = out[uid]
        r.setdefault("email", (_get(e, ("referrer", "email")) or "").lower())
        r.setdefault("nombre", f"{_get(e, ('referrer', 'firstName')) or ''} {_get(e, ('referrer', 'lastName')) or ''}".strip())
        if e.get("credit"):  # pago hecho al referidor
            r["cobrado"] += e["credit"]
            continue
        v = e.get("value") or 0
        if not v:
            continue
        rc, xc = _get(ra, ("company", "_id")), _get(e, ("agent", "company", "_id"))
        rn, xn = norm_company(_get(ra, ("company", "name"))), norm_company(_get(e, ("agent", "company", "name")))
        propia = ra.get("type") == "master" and ((rc and rc == xc) or (rn and rn == xn))
        st = e.get("status")
        if st == "available":
            if propia:
                r["excluido_mb"] += v
            else:
                r["disponible"] += v
                r["entradas"].append({"id": str(e["_id"]), "monto": round(v, 2), "mes": e.get("close"),
                                      "operacion": e.get("operation"),
                                      "referido": f"{_get(e, ('agent', 'firstName')) or ''} {_get(e, ('agent', 'lastName')) or ''}".strip(),
                                      "nivel": _get(e, ("referrer", "level"))})
        elif st == "payment-pending":
            r["en_proceso"] += v
    for r in out.values():
        for k in ("disponible", "excluido_mb", "en_proceso", "cobrado"):
            r[k] = round(r[k], 2)
    return dict(out)
