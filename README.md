# Transparencia fiscal: ejecución presupuestaria DIPRES → MongoDB

Script en Python que descarga los informes de ejecución presupuestaria publicados por la
[Dirección de Presupuestos (DIPRES)](https://www.dipres.gob.cl/) de Chile (CSV mensuales y
trimestrales, en pesos y dólares) y los carga en MongoDB.

## Instalación

```bash
pip install -r requirements.txt
```

## Conexión a MongoDB

La cadena de conexión se toma de la variable de entorno `MONGODB_URI` (nunca la guardes en el
repositorio). Ejemplo con MongoDB Atlas:

```bash
read -s -p "Clave de Atlas: " DBPASS; echo
export MONGODB_URI="mongodb+srv://USUARIO:$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$DBPASS")@TU-CLUSTER.mongodb.net/?retryWrites=true&w=majority"
unset DBPASS
```

En Atlas, agrega tu IP en *Network Access* o la conexión fallará por timeout.

## Uso

```bash
python dipres_ejecucion.py --anios 2026 --moneda Pesos --seco   # descarga y parsea, no carga
python dipres_ejecucion.py --anios 2026 --moneda Pesos          # carga en MongoDB
python dipres_ejecucion.py --anios 2024-2026                    # varios años, ambas monedas
python dipres_ejecucion.py --anios 2026 --solo-descargar        # solo guarda los CSV
python dipres_ejecucion.py --secciones todas --anios 2026       # incluye Ley Regular, FET-Incendios y Gores
```

Opciones principales: `--anios`, `--secciones`, `--moneda {Pesos,Dolares,ambas}`, `--cache`,
`--db` (por defecto `transparencia_fiscal`), `--seco`, `--solo-descargar`, `--forzar`.
Para probar con un CSV ya descargado: `--archivo ruta.csv --anio 2026 --periodo "Tercer Trimestre" --moneda Pesos`.

El proceso es idempotente: cada archivo se identifica por su id de artículo en DIPRES y se omite
si ya fue cargado con la misma marca `ts`. Si DIPRES republica un informe, se recarga.

## Modelo de datos

Colecciones en la base `transparencia_fiscal`:

- `ejecucion`: una fila del CSV por documento, con `anio`, `seccion`, `periodo`, `mes_cierre`,
  `moneda`, `partida`, `capitulo`, `programa`, `subtitulo`, `item`, `asignacion`, `denominacion`,
  `presupuesto_inicial`, `presupuesto_vigente`, `ejecucion_acumulada`, `nivel`, `archivo_id`.
- `cargas`: registro de cada archivo cargado (url, `ts`, sha256, filas).

Puntos a tener en cuenta:

- Las filas son **jerárquicas** (un subtítulo agrupa a sus ítems y un ítem a sus asignaciones).
  Para no duplicar montos, filtra por `nivel` (`subtitulo`, `item`, `asignacion`, `resultado`).
- Las filas `RESULTADO` vienen sin partida, capítulo ni programa en el CSV; se les asigna el
  programa de la fila anterior.
- Algunos montos vienen en notación científica con precisión perdida en el origen; esas filas
  quedan con `valor_aproximado: true`.
- Los CSV de DIPRES usan `;` como separador y codificación Windows-1252.
- El plan gratuito de Atlas (M0) tiene 512 MB. Un CSV de ~1 MB ocupa ~6,6 MB como BSON sin
  comprimir; revisa `db.stats().storageSize` antes de cargar muchos años.

## Limitaciones conocidas

- Solo se detectan años desde 2011 (los anteriores usan otro formato de URL).
- Solo se leen los enlaces presentes en el HTML inicial de cada página; las pestañas que DIPRES
  carga dinámicamente no se exploran.
- Los datos pertenecen a DIPRES; revisa sus condiciones de uso antes de redistribuirlos.
