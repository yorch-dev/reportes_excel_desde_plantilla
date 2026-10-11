"""Agregación diaria de series nativas, antes de reducirlas a treinta minutos."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def limites_dia(fecha: date, zona: str) -> tuple[int, int]:
    tz = ZoneInfo(zona)
    return (int(datetime.combine(fecha, time.min, tz).timestamp()),
            int(datetime.combine(fecha + timedelta(days=1), time.min, tz).timestamp()))


def _slots(fecha: date, zona: str, raster: int) -> tuple[list[int], bool]:
    inicio, fin = limites_dia(fecha, zona)
    if raster not in (900, 1800, 3600, 86400):
        return [], False
    # P1D sólo es inequívoco cuando representa exactamente el día local.
    if (fin - inicio) % raster:
        return [], False
    return list(range(inicio, fin, raster)), True


def _cobertura(valores: list, esperado: int, estado: str | None = None) -> dict:
    conocidos = [v for v in valores if v is not None]
    if estado is None:
        estado = "sin_datos" if not conocidos else "completo" if len(conocidos) == esperado else "parcial"
    return {"valores": len(conocidos), "ceros": sum(v == 0 for v in conocidos),
            "vacios": esperado - len(conocidos), "esperados": esperado, "estado": estado}


def agregar_volumen_tmo_dia(volumen: dict, tmo: dict, raster: int, fecha: date,
                            zona: str, raster_tmo: int | None = None) -> tuple[float | None, float | None, dict, dict]:
    """Sumar volumen observado; TMO usa los mismos pesos positivos observados.

    Los días parciales conservan su volumen observado con cobertura explícita.
    El TMO nunca omite un peso positivo cuyo tiempo falte. Cada variante usa sus
    propias ofrecidas; la llamada histórica usa sus propias contestadas.
    """
    slots, valido = _slots(fecha, zona, raster)
    if not valido:
        cov = _cobertura([], 0, "raster_no_representa_dia_local")
        return None, None, cov, dict(cov)
    inicio, fin = limites_dia(fecha, zona)
    # Un punto diario UTC no se atribuye a un día local distinto ni se reparte.
    desalineado = raster == 86400 and any(inicio <= ts < fin and ts != inicio for ts in volumen)
    if desalineado:
        cov = _cobertura([], len(slots), "serie_diaria_no_alineada_al_dia_local")
        return None, None, cov, dict(cov)
    q = [volumen.get(ts) for ts in slots]
    a = [tmo.get(ts) for ts in slots]
    cv = _cobertura(q, len(slots))
    total = sum(v for v in q if v is not None) if cv["valores"] else None
    ca = _cobertura(a, len(slots))
    peso = sum(v for v in q if v is not None and v > 0)
    ca.update(peso=peso, pesos_positivos=sum(v is not None and v > 0 for v in q),
              tmo_faltante_peso_positivo=sum(v is not None and v > 0 and h is None for v, h in zip(q, a)))
    if raster_tmo is not None and raster_tmo != raster:
        ca["estado"] = "raster_tmo_distinto_peso"
    elif any(v is not None and v < 0 for v in q):
        ca["estado"] = "pesos_negativos"
    elif cv["valores"] == 0:
        ca["estado"] = "sin_pesos"
    elif peso == 0:
        ca["estado"] = "peso_cero"
    elif ca["tmo_faltante_peso_positivo"]:
        ca["estado"] = "tmo_faltante_peso_positivo"
    else:
        ca["estado"] = "ponderado_parcial" if cv["vacios"] else "completo"
        return total, sum(v * h for v, h in zip(q, a) if v is not None and v > 0) / peso, cv, ca
    return total, None, cv, ca


def agregar_requerido_dia(valores: dict, raster: int, fecha: date,
                          zona: str) -> tuple[float | None, dict]:
    slots, valido = _slots(fecha, zona, raster)
    if not valido:
        return None, _cobertura([], 0, "raster_no_representa_dia_local")
    q = [valores.get(ts) for ts in slots]
    cov = _cobertura(q, len(slots))
    cov["horas_cubiertas"] = cov["valores"] * raster / 3600
    cov["horas_dia"] = (limites_dia(fecha, zona)[1] - limites_dia(fecha, zona)[0]) / 3600
    return (sum(v * raster / 3600 for v in q if v is not None) if cov["valores"] else None), cov
