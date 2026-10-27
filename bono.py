"""Bono 1·5·10 (Laura, 27-oct-2026).

- Propiedad 1·5·10 = exclusiva de Pulppo (`properties.contract.exclusive.pulppo` = fecha de alta al programa),
  con comisión de 5 % + IVA (≥ 5 % sin IVA) y exclusiva de al menos 6 meses.
- Bono = 5 % de lo que se lleva el bróker comprador de Pulppo: ambos lados → 50 % de la comisión; lado comprador → 100 %.
  Lado vendedor o productor: no aplica. Lo cobra el `buyer.broker`, sea asesor o Master Broker.
- Se genera cuando la operación está concretada (`closed`) y se puede cobrar desde el mes siguiente al cierre, sin plazo.
  `paying` (en cobranza) espera a cerrarse. Cuentan los cierres desde `bono_desde` (1-oct-2026).
- Una factura por operación, concepto "Bono 1.5.10 <ID de la operación>". Finanzas/KAM aprueban o rechazan; Finanzas paga.

cargar(db, ...) lee Mongo; calcular(ops, ...) aplica estados sin Mongo.
"""
from datetime import date, datetime, timezone

import calculo
from calculo import MX, _get

ESTADO_GRUPO = {"disponible": "a_cobrar", "facturado": "en_proceso", "aprobado": "en_proceso", "rechazado": "en_proceso",
                "pagado": "cobrado", "en_curso": "a_futuro", "en_cobranza": "a_futuro", "excluido": "no_aplica"}


def comision_sin_iva(contrato):
    """% de comisión cargado, sin IVA. Si se capturó con IVA (ivaIncluded, o 5.8 = 5 × 1.16) se le quita."""
    c = contrato.get("comission")
    if not isinstance(c, (int, float)):
        return None
    if contrato.get("ivaIncluded"):
        return c / 1.16
    if c != round(c) and abs(c / 1.16 - round(c / 1.16)) < 0.005:
        return round(c / 1.16, 2)
    return c


def requisitos(prop, op, cfg):
    """(motivos que impiden el bono, advertencias para quien aprueba)."""
    con = prop.get("contract") or {}
    ex = con.get("exclusive") or {}
    motivos, avisos = [], []
    pct = comision_sin_iva(con)
    if pct is None or pct < cfg["bono_comision_min"] - 0.005:
        motivos.append(f"Comisión de la propiedad {pct:.2f} % sin IVA (debe ser 5 % + IVA)" if pct is not None
                       else "La propiedad no tiene comisión cargada")
    dur = ex.get("durationMonths")
    if not dur:
        motivos.append("La exclusiva no tiene duración cargada")
    elif dur < cfg["bono_meses_exclusiva"]:
        motivos.append(f"Exclusiva de {dur} meses (mínimo {cfg['bono_meses_exclusiva']})")
    alta, cierre = ex.get("pulppo"), op.get("closedAt")
    if alta and cierre and alta > cierre:
        motivos.append(f"Se dio de alta en 1·5·10 el {alta.astimezone(MX):%d/%m/%Y}, después del cierre")
    if con.get("status") != "completed":
        avisos.append(f"Contrato de exclusiva sin firmar en la plataforma (estado: {con.get('status') or 'sin estado'})")
    return motivos, avisos


def cargar(db, cfg, excluidas=()):
    """Operaciones 1·5·10 con bróker comprador de Pulppo, cerradas desde cfg['bono_desde'] (incluye las que no aplican, con motivo)."""
    desde = date.fromisoformat(cfg["bono_desde"])
    ini = datetime(desde.year, desde.month, desde.day, 6, tzinfo=timezone.utc)  # 00:00 hora de México
    props = {p["_id"]: p for p in db.properties.find(
        {"contract.exclusive.pulppo": {"$exists": True, "$ne": None}},
        {"internalId": 1, "contract.comission": 1, "contract.ivaIncluded": 1, "contract.status": 1, "contract.exclusive": 1,
         "address.neighborhood.name": 1})}
    q = {"property._id": {"$in": list(props)}, "status.last": {"$in": ["closed", "paying"]}, "side": {"$in": ["both", "buyer"]},
         "closedAt": {"$gte": ini}}
    ops = list(db.operations.find(q, {"id": 1, "side": 1, "status.last": 1, "closedAt": 1, "comission": 1, "closeValue": 1,
                                      "buyer.broker._id": 1, "buyer.company": 1, "buyer.contact._id": 1, "property._id": 1}))
    bids = {_get(o, ("buyer", "broker", "_id")) for o in ops} - {None}
    cursos = [c["_id"] for c in db.courses.find({"level": {"$lte": 1}, "isActive": True}, {"_id": 1})]
    agentes = {a["_id"]: calculo._agente(a, cursos) for a in db.agents.find(
        {"_id": {"$in": list(bids)}}, {"firstName": 1, "lastName": 1, "email": 1, "type": 1, "status": 1, "deletedAt": 1,
                                       "company": 1, "courses": 1, "createdAt": 1, "level": 1, "uid": 1})}
    from bson import ObjectId
    companias = {str(c["_id"]): calculo._compania(c, excluidas) for c in db.companies.find(
        {"_id": {"$in": [ObjectId(a["company_id"]) for a in agentes.values() if a["company_id"]]}},
        {"name": 1, "email": 1, "status": 1, "external": 1})}

    # Una venta puede venir duplicada (paying + closed): se queda la cerrada
    vistos = {}
    for o in sorted(ops, key=lambda o: o["status"]["last"] != "closed"):
        k = (o["property"]["_id"], _get(o, ("buyer", "contact", "_id")), _get(o, ("buyer", "broker", "_id")))
        vistos.setdefault(k, o)

    out = []
    for o in vistos.values():
        p = props[o["property"]["_id"]]
        b = agentes.get(_get(o, ("buyer", "broker", "_id")))
        motivos, avisos = requisitos(p, o, cfg)
        if _get(o, ("buyer", "company", "external")):
            motivos.append("El comprador lo trajo una inmobiliaria externa")
        if not b:
            motivos.append("Sin bróker comprador")
        elif not b["activo"]:
            motivos.append("El bróker comprador no está activo")
        elif not calculo.participa(b, companias):
            c = companias.get(b["company_id"]) or {}
            motivos.append(f"La inmobiliaria del bróker no participa ({c.get('motivo_exclusion') or 'sin inmobiliaria'})")
        if _get(o, ("comission", "currency")) == "USD":
            motivos.append("Comisión en dólares: revisar con Finanzas")
        fr = 0.5 if o.get("side") == "both" else 1.0
        com = _get(o, ("comission", "value")) or 0
        base = round(com * fr, 2)
        cierre = o["closedAt"].astimezone(MX).date()
        out.append({
            "id": str(o["_id"]), "operacion": o.get("id") or str(o["_id"]), "estado_op": o["status"]["last"], "lado": o.get("side"),
            "cierre": cierre.isoformat(), "propiedad": p.get("internalId"), "colonia": _get(p, ("address", "neighborhood", "name")),
            "valor": _get(o, ("closeValue", "value")), "comision": round(com, 2), "fraccion": fr, "base": base,
            "porcentaje": cfg["bono_porcentaje"], "monto": round(base * cfg["bono_porcentaje"], 2),
            "comision_propiedad": comision_sin_iva(p.get("contract") or {}),
            "exclusiva_meses": _get(p, ("contract", "exclusive", "durationMonths")),
            "broker_id": b and b["id"], "broker": b and b["nombre"], "broker_email": b and b["email"],
            "broker_tipo": b and b["tipo"], "inmobiliaria": b and b["company_nombre"],
            "motivos": motivos, "avisos": avisos,
        })
    out.sort(key=lambda x: x["cierre"], reverse=True)
    return out


def concepto(op):
    return f"Bono 1.5.10 {op}"


def calcular(ops, hoy, solicitudes=()):
    """Estado de cada operación: excluido · en_cobranza · en_curso (se cobra desde el mes siguiente) · disponible ·
    facturado / aprobado / rechazado / pagado (según su factura)."""
    por_op = {}
    for s in solicitudes:  # la más reciente primero; una rechazada sigue viva hasta que se corrige
        for l in s["lineas"]:
            por_op.setdefault(l["id"], s)
    out = []
    for o in ops:
        x = dict(o, motivo=None, solicitud_id=None, cobrable_desde=None)
        c = date.fromisoformat(o["cierre"])
        desde = calculo.dia(calculo.mes_mas(f"{c.year:04d}-{c.month:02d}", 1), 1)
        s = por_op.get(o["id"])
        if o["motivos"]:
            x["estado"], x["motivo"] = "excluido", " · ".join(o["motivos"])
        elif s:
            x["estado"], x["solicitud_id"] = calculo.estado_solicitud(s, hoy, {}), s["id"]
        elif o["estado_op"] != "closed":
            x["estado"], x["motivo"] = "en_cobranza", "La operación sigue en cobranza; el bono se genera cuando se concrete"
        elif hoy < desde:
            x["estado"], x["cobrable_desde"] = "en_curso", desde.isoformat()
            x["motivo"] = f"Se puede cobrar a partir del {calculo.fecha_txt(desde)}"
        else:
            x["estado"], x["cobrable_desde"] = "disponible", desde.isoformat()
        x["grupo"] = ESTADO_GRUPO.get(x["estado"], "a_futuro")
        out.append(x)
    return out


def resumen(lineas):
    t = {g: 0.0 for g in ("a_cobrar", "en_proceso", "cobrado", "a_futuro")}
    for l in lineas:
        if l["grupo"] in t:
            t[l["grupo"]] += l["monto"]
    return {k: round(v, 2) for k, v in t.items()}
