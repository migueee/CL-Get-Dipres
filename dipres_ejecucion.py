#!/usr/bin/env python3
"""
Descarga los informes de ejecucion presupuestaria de DIPRES (CSV mensuales y
trimestrales) y los carga en MongoDB.

Uso rapido
----------
    pip install -r requirements.txt
    export MONGODB_URI="mongodb+srv://USUARIO:CLAVE@cluster.xxxxx.mongodb.net"

    python dipres_ejecucion.py --anios 2026                  # descarga y carga
    python dipres_ejecucion.py --anios 2024-2026 --moneda Pesos
    python dipres_ejecucion.py --anios 2026 --solo-descargar # solo guarda los CSV
    python dipres_ejecucion.py --anios 2026 --seco           # parsea, no carga
    python dipres_ejecucion.py --archivo informe.csv --anio 2026 \
        --periodo "Tercer Trimestre" --moneda Pesos --seco   # prueba con un CSV local

El script es idempotente: cada archivo se identifica por su id de articulo en
DIPRES. Si ya fue cargado con la misma marca `ts` se omite; si DIPRES lo
republica (cambia `ts`) se descarga de nuevo y se reemplazan sus filas.

Notas sobre los datos
---------------------
* Las filas son jerarquicas: un subtitulo contiene a sus items y un item a sus
  asignaciones. Para no duplicar montos, filtra por el campo `nivel`
  ("subtitulo", "item", "asignacion", "resultado"). Ejemplo:
      db.ejecucion.aggregate([
        {"$match": {"anio": 2026, "periodo": "Tercer Trimestre",
                    "moneda": "Pesos", "nivel": "subtitulo"}},
        {"$group": {"_id": "$subtitulo",
                    "ejecucion": {"$sum": "$ejecucion_acumulada"}}}])
* Las filas "RESULTADO" vienen sin partida/capitulo/programa en el CSV; aqui se
  les asigna el programa de la fila inmediatamente anterior.
* Algunos montos vienen en notacion cientifica (p. ej. 1,36187E+11), con
  precision perdida en el origen; esas filas quedan con `valor_aproximado: true`.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("dipres")

BASE = "https://www.dipres.gob.cl/597/"
SEMILLA = BASE + "w3-multipropertyvalues-25910-37782.html"  # Ejecucion Total 2026
PAUSA_SEGUNDOS = 0.7  # cortesia entre solicitudes al sitio

# Secciones de "Presupuesto" con informes de ejecucion (id usado en la URL).
SECCIONES = {
    "ejecucion-total": 25910,
    "ejecucion-ley-regular": 25916,
    "ejecucion-fet-incendios": 36912,
    "ejecucion-gores": 35610,
}

# Ids de anio observados en el selector "Ver otros anios" (respaldo si falla
# el descubrimiento automatico).
ANIOS_RESPALDO = {
    2027: 38403, 2026: 37782, 2025: 36882, 2024: 35869, 2023: 35324,
    2022: 34905, 2021: 25771, 2020: 25190, 2019: 24532, 2018: 24043,
    2017: 23712, 2016: 22940, 2015: 22369, 2014: 22027, 2013: 21672,
    2012: 21327, 2011: 20971,
}

RE_ANIO_HREF = re.compile(r"w3-multipropertyvalues-15199-(\d+)\.html")
RE_CSV = re.compile(r"articles-(\d+)_doc_csv\.csv")
# Tolerante a tildes mal decodificadas (p. ej. "EjecuciÃ³n", "DÃ³lares").
RE_ENCABEZADO = re.compile(
    r"Informe\s+Ejecuci.{1,3}n\s+(?:Acumulada\s+)?(?:a\s+)?(?P<periodo>.+?)\s*"
    r"\[(?P<moneda>Pesos|D.{1,3}lares)\]",
    re.IGNORECASE,
)
RE_ENTERO = re.compile(r"-?\d+")

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
TRIMESTRES = {"primer": 3, "segundo": 6, "tercer": 9, "cuarto": 12}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def sin_tildes(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def mes_de_cierre(periodo: str) -> int | None:
    """'Enero' -> 1, 'Tercer Trimestre' -> 9 (la ejecucion es acumulada)."""
    p = sin_tildes(periodo)
    if "trimestre" in p:
        for nombre, mes in TRIMESTRES.items():
            if nombre in p:
                return mes
    for nombre, mes in MESES.items():
        if nombre in p:
            return mes
    return None


def codigo(valor: str):
    """Codigos presupuestarios: entero si es numerico; None si viene vacio."""
    v = valor.strip()
    if v == "":
        return None
    return int(v) if v.isdigit() else v


def parse_num(valor: str):
    """Devuelve (numero | None, es_notacion_cientifica)."""
    s = valor.strip()
    if s == "":
        return None, False
    if RE_ENTERO.fullmatch(s):
        return int(s), False
    t = s.replace(" ", "")
    cientifica = "e" in t.lower()
    if "," in t and "." in t:  # 1.234,56 -> 1234.56
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:  # 1,36187E+11 -> 1.36187E+11
        t = t.replace(",", ".")
    return float(t), cientifica


def decodificar(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


# --------------------------------------------------------------------------
# Descubrimiento de enlaces
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Enlace:
    anio: int
    seccion: str
    periodo: str
    mes_cierre: int | None
    moneda: str  # "Pesos" | "Dolares"
    documento: str
    article_id: int
    url: str
    ts: str


def nueva_sesion() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.5",
    })
    retry = Retry(
        total=4, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def obtener(sesion: requests.Session, url: str) -> requests.Response:
    time.sleep(PAUSA_SEGUNDOS)
    resp = sesion.get(url, timeout=60)
    resp.raise_for_status()
    return resp


def obtener_html(sesion: requests.Session, url: str) -> str:
    """Descarga una pagina HTML decodificandola como UTF-8 (el sitio la declara
    en un <meta>, y requests a veces asume ISO-8859-1 y rompe las tildes)."""
    resp = obtener(sesion, url)
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode("cp1252", errors="replace")


def diagnosticar(html: str, anio: int, seccion: str, cache: Path) -> None:
    """Cuando no se encuentran enlaces: guarda el HTML y resume lo que contiene."""
    cache.mkdir(parents=True, exist_ok=True)
    ruta = cache / f"debug_{anio}_{seccion}.html"
    ruta.write_text(html, encoding="utf-8")
    log.warning("   DIAGNOSTICO: HTML guardado en %s (%d caracteres)", ruta, len(html))
    log.warning("   apariciones: '.csv'=%d  'doc_csv'=%d  'Informe Ejec'=%d  "
                "'[Pesos]'=%d  'Cargando'=%d",
                html.count(".csv"), html.count("doc_csv"),
                len(re.findall(r"Informe\s+Ejecuci", html)),
                html.count("[Pesos]"), html.count("Cargando"))
    for m in list(re.finditer(r"doc_csv|\.csv", html))[:2]:
        ini = max(0, m.start() - 160)
        log.warning("   contexto: ...%s...", html[ini:m.end() + 60].replace("\n", " "))
    if "csv" not in html.lower():
        log.warning("   La pagina no trae enlaces CSV en el HTML inicial; "
                    "probablemente se cargan por JavaScript. Comparte este "
                    "diagnostico para ajustar el script.")


def descubrir_anios(sesion: requests.Session) -> dict[int, int]:
    """Lee el selector 'Ver otros anios' para mapear anio -> id de pagina."""
    try:
        soup = BeautifulSoup(obtener_html(sesion, SEMILLA), "html.parser")
        ids = {}
        for a in soup.find_all("a", href=True):
            m = RE_ANIO_HREF.search(a["href"])
            texto = a.get_text(strip=True)
            if m and re.fullmatch(r"\d{4}", texto):
                ids[int(texto)] = int(m.group(1))
        if ids:
            return ids
        log.warning("No se encontro el selector de anios; uso lista de respaldo")
    except requests.RequestException as exc:
        log.warning("No se pudo leer la pagina semilla (%s); uso lista de respaldo", exc)
    return dict(ANIOS_RESPALDO)


def url_pagina(seccion: str, anio_id: int) -> str:
    return f"{BASE}w3-multipropertyvalues-{SECCIONES[seccion]}-{anio_id}.html"


def extraer_enlaces(html: str, pagina: str, anio: int, seccion: str) -> list[Enlace]:
    """
    Recorre el HTML en orden de documento: cada encabezado del tipo
    'Informe Ejecucion a Enero [Pesos]' fija el periodo y la moneda de los
    enlaces CSV que le siguen.
    """
    soup = BeautifulSoup(html, "html.parser")
    actual = None  # (periodo, moneda)
    vistos: dict[int, Enlace] = {}
    for nodo in soup.descendants:
        if isinstance(nodo, NavigableString):
            m = RE_ENCABEZADO.search(str(nodo))
            if m:
                moneda = "Pesos" if m["moneda"].lower().startswith("p") else "Dolares"
                actual = (m["periodo"].strip(), moneda)
        elif getattr(nodo, "name", None) == "a" and nodo.get("href"):
            m = RE_CSV.search(nodo["href"])
            if not (m and actual):
                continue
            article_id = int(m.group(1))
            if article_id in vistos:
                continue
            url = urljoin(pagina, nodo["href"])
            etiqueta = nodo.get_text(" ", strip=True)
            documento = re.sub(r"^.*?:\s*", "", etiqueta)
            documento = re.sub(r"\s*\[.*?\]\s*$", "", documento).strip()
            ts = parse_qs(urlparse(url).query).get("ts", [""])[0]
            periodo, moneda = actual
            vistos[article_id] = Enlace(
                anio=anio, seccion=seccion, periodo=periodo,
                mes_cierre=mes_de_cierre(periodo), moneda=moneda,
                documento=documento, article_id=article_id, url=url, ts=ts,
            )
    return list(vistos.values())


# --------------------------------------------------------------------------
# Descarga y parseo
# --------------------------------------------------------------------------
def descargar(sesion: requests.Session, e: Enlace, cache: Path) -> bytes:
    ruta = cache / str(e.anio) / e.seccion / f"{e.article_id}_{e.ts or 'sin-ts'}.csv"
    if ruta.exists():
        return ruta.read_bytes()
    contenido = obtener(sesion, e.url).content
    if contenido.lstrip()[:1] == b"<":
        raise ValueError(f"{e.url} devolvio HTML en vez de CSV")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(contenido)
    return contenido


def nombre_columna(encabezado: str) -> str:
    h = sin_tildes(encabezado)
    if h in ("partida", "capitulo", "programa", "subtitulo", "item", "asignacion"):
        return h
    if h == "moneda":
        return "moneda_codigo"
    if h.startswith("denominacion"):
        return "denominacion"
    if h.startswith("presupuesto inicial"):
        return "presupuesto_inicial"
    if h.startswith("presupuesto vigente"):
        return "presupuesto_vigente"
    if h.startswith("ejecucion"):
        return "ejecucion_acumulada"
    return "otros:" + re.sub(r"\W+", "_", h).strip("_")


COLUMNAS_REQUERIDAS = {
    "partida", "capitulo", "programa", "subtitulo", "item", "asignacion",
    "denominacion",
}
COLUMNAS_CODIGO = ("partida", "capitulo", "programa", "subtitulo", "item", "asignacion")


def parsear_csv(texto: str, e: Enlace) -> tuple[list[dict], dict]:
    lector = csv.reader(io.StringIO(texto), delimiter=";")
    encabezado = next(lector)
    nombres, usados = [], set()
    for h in encabezado:
        n = nombre_columna(h)
        if n in usados:  # columna repetida: no pisar la anterior
            n = "otros:" + re.sub(r"\W+", "_", sin_tildes(h)).strip("_")
        usados.add(n)
        nombres.append(n)
    faltan = COLUMNAS_REQUERIDAS - set(nombres)
    if faltan:
        raise ValueError(f"Faltan columnas {sorted(faltan)}; encabezado: {encabezado}")

    base = {
        "anio": e.anio, "seccion": e.seccion, "periodo": e.periodo,
        "mes_cierre": e.mes_cierre, "moneda": e.moneda,
        "documento": e.documento, "archivo_id": e.article_id,
    }
    stats: Counter = Counter()
    filas: list[dict] = []
    anterior = None  # (partida, capitulo, programa) de la ultima fila normal

    for n_linea, fila in enumerate(lector, start=2):
        if not any(c.strip() for c in fila):
            continue
        if len(fila) != len(nombres):
            raise ValueError(
                f"Linea {n_linea}: {len(fila)} columnas, se esperaban {len(nombres)}"
            )
        doc = dict(base)
        aproximado = False
        otros = {}
        for nombre, valor in zip(nombres, fila):
            if nombre in COLUMNAS_CODIGO:
                doc[nombre] = codigo(valor)
            elif nombre == "denominacion":
                doc[nombre] = valor.strip()
            elif nombre == "moneda_codigo":
                doc[nombre] = valor.strip() or None
            else:  # columnas de montos
                try:
                    num, cientifica = parse_num(valor)
                except ValueError:
                    num, cientifica = valor.strip() or None, False
                aproximado = aproximado or cientifica
                if nombre.startswith("otros:"):
                    otros[nombre[6:]] = num
                else:
                    doc[nombre] = num
        if otros:
            doc["otros"] = otros

        es_resultado = (
            doc["partida"] is None and doc["capitulo"] is None
            and doc["programa"] is None
            and doc["denominacion"].upper() == "RESULTADO"
        )
        if es_resultado:
            if anterior:
                doc["partida"], doc["capitulo"], doc["programa"] = anterior
            else:
                stats["resultado_sin_programa_previo"] += 1
            doc["nivel"] = "resultado"
            doc["es_resultado"] = True
            stats["resultado"] += 1
        else:
            if doc["partida"] is None:
                stats["sin_partida"] += 1
            else:
                anterior = (doc["partida"], doc["capitulo"], doc["programa"])
            if doc["asignacion"] is not None:
                doc["nivel"] = "asignacion"
            elif doc["item"] is not None:
                doc["nivel"] = "item"
            elif doc["subtitulo"] is not None:
                doc["nivel"] = "subtitulo"
            else:
                doc["nivel"] = "programa"
        if aproximado:
            doc["valor_aproximado"] = True
            stats["valor_aproximado"] += 1

        filas.append({k: v for k, v in doc.items() if v is not None})

    stats["filas"] = len(filas)
    return filas, dict(stats)


# --------------------------------------------------------------------------
# MongoDB
# --------------------------------------------------------------------------
def conectar_mongo(uri: str, nombre_db: str):
    from pymongo import MongoClient  # import perezoso: --seco no lo necesita

    cliente = MongoClient(uri, serverSelectionTimeoutMS=15000)
    cliente.admin.command("ping")
    return cliente[nombre_db]


def asegurar_indices(db) -> None:
    col = db["ejecucion"]
    col.create_index("archivo_id")
    col.create_index([("anio", 1), ("mes_cierre", 1), ("moneda", 1), ("nivel", 1)])
    col.create_index([("partida", 1), ("capitulo", 1), ("programa", 1), ("subtitulo", 1)])


def ya_cargado(db, e: Enlace) -> bool:
    return db["cargas"].find_one({"_id": e.article_id, "ts": e.ts}) is not None


def cargar(db, e: Enlace, filas: list[dict], sha256: str, stats: dict) -> None:
    col = db["ejecucion"]
    col.delete_many({"archivo_id": e.article_id})  # recarga limpia del archivo
    for i in range(0, len(filas), 5000):
        col.insert_many(filas[i:i + 5000], ordered=False)
    db["cargas"].replace_one(
        {"_id": e.article_id},
        {
            "_id": e.article_id, "anio": e.anio, "seccion": e.seccion,
            "periodo": e.periodo, "moneda": e.moneda, "documento": e.documento,
            "url": e.url, "ts": e.ts, "sha256": sha256, "filas": len(filas),
            "stats": stats, "cargado_en": dt.datetime.now(dt.timezone.utc),
        },
        upsert=True,
    )


# --------------------------------------------------------------------------
# Orquestacion
# --------------------------------------------------------------------------
def procesar_contenido(db, e: Enlace, raw: bytes, seco: bool) -> None:
    filas, stats = parsear_csv(decodificar(raw), e)
    log.info("   %s %s [%s]: %d filas %s", e.anio, e.periodo, e.moneda,
             len(filas), {k: v for k, v in stats.items() if k != "filas"})
    if not seco:
        cargar(db, e, filas, hashlib.sha256(raw).hexdigest(), stats)


def parsear_anios(texto: str) -> list[int]:
    anios: set[int] = set()
    for parte in texto.split(","):
        parte = parte.strip()
        if "-" in parte:
            a, b = parte.split("-", 1)
            anios.update(range(int(a), int(b) + 1))
        elif parte:
            anios.add(int(parte))
    return sorted(anios)


def leer_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--anios", default=str(dt.date.today().year),
                   help="'2026', '2024-2026' o '2022,2024' (defecto: anio actual)")
    p.add_argument("--secciones", default="ejecucion-total",
                   help="lista separada por comas o 'todas'. Opciones: "
                        + ", ".join(SECCIONES))
    p.add_argument("--moneda", choices=("Pesos", "Dolares", "ambas"), default="ambas")
    p.add_argument("--cache", default="datos_dipres", help="carpeta para los CSV")
    p.add_argument("--mongo-uri", default=None, help="o variable MONGODB_URI")
    p.add_argument("--db", default="transparencia_fiscal")
    p.add_argument("--solo-descargar", action="store_true")
    p.add_argument("--seco", action="store_true", help="parsea pero no carga")
    p.add_argument("--forzar", action="store_true", help="recarga aunque ya este cargado")
    g = p.add_argument_group("prueba con un CSV local")
    g.add_argument("--archivo")
    g.add_argument("--anio", type=int)
    g.add_argument("--periodo")
    g.add_argument("--seccion", default="ejecucion-total")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = leer_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db = None
    if not (args.seco or args.solo_descargar):
        uri = args.mongo_uri or os.environ.get("MONGODB_URI")
        if not uri:
            sys.exit("Falta la cadena de conexion: define MONGODB_URI o usa --mongo-uri "
                     "(o prueba con --seco / --solo-descargar).")
        db = conectar_mongo(uri, args.db)
        asegurar_indices(db)

    errores = 0

    # ---- Modo archivo local -------------------------------------------
    if args.archivo:
        if not (args.anio and args.periodo and args.moneda != "ambas"):
            sys.exit("--archivo requiere --anio, --periodo y --moneda (Pesos|Dolares)")
        ruta = Path(args.archivo)
        m = re.search(r"articles-(\d+)", ruta.name)
        e = Enlace(
            anio=args.anio, seccion=args.seccion, periodo=args.periodo,
            mes_cierre=mes_de_cierre(args.periodo), moneda=args.moneda,
            documento="archivo local", article_id=int(m.group(1)) if m else 0,
            url=str(ruta), ts="local",
        )
        procesar_contenido(db, e, ruta.read_bytes(), args.seco or args.solo_descargar)
        return 0

    # ---- Modo web -------------------------------------------------------
    sesion = nueva_sesion()
    ids_anio = descubrir_anios(sesion)
    secciones = list(SECCIONES) if args.secciones == "todas" else [
        s.strip() for s in args.secciones.split(",") if s.strip()
    ]
    for s in secciones:
        if s not in SECCIONES:
            sys.exit(f"Seccion desconocida: {s}. Opciones: {', '.join(SECCIONES)}")
    cache = Path(args.cache)
    total = 0

    for anio in parsear_anios(args.anios):
        if anio not in ids_anio:
            log.warning("Anio %s no esta en el selector de DIPRES; lo omito", anio)
            continue
        for seccion in secciones:
            pagina = url_pagina(seccion, ids_anio[anio])
            log.info("== %s / %s", anio, seccion)
            try:
                html = obtener_html(sesion, pagina)
            except requests.RequestException as exc:
                log.warning("   no se pudo abrir %s (%s)", pagina, exc)
                continue
            enlaces = extraer_enlaces(html, pagina, anio, seccion)
            if args.moneda != "ambas":
                enlaces = [x for x in enlaces if x.moneda == args.moneda]
            if not enlaces:
                log.warning("   sin enlaces CSV en el HTML estatico de %s", pagina)
                diagnosticar(html, anio, seccion, cache)
                continue
            for e in enlaces:
                try:
                    if db is not None and not args.forzar and ya_cargado(db, e):
                        log.info("   %s [%s] ya cargado (mismo ts), omito",
                                 e.periodo, e.moneda)
                        continue
                    raw = descargar(sesion, e, cache)
                    total += 1
                    if args.solo_descargar:
                        log.info("   descargado %s [%s] (%d bytes)",
                                 e.periodo, e.moneda, len(raw))
                        continue
                    procesar_contenido(db, e, raw, args.seco)
                except Exception as exc:  # un archivo malo no detiene al resto
                    errores += 1
                    log.error("   fallo %s [%s] (%s): %s", e.periodo, e.moneda,
                              e.url, exc)

    log.info("Listo: %d archivos procesados, %d con error", total, errores)
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())
