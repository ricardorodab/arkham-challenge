# Parte 2: cómo ejecutarla y explicar el diseño

La Parte 2 convierte las tres extracciones de EIA en un modelo verificable. El código, los Parquet reales y la documentación están en este proyecto. No necesita una nueva descarga ni una API key para consultar, verificar o reproducir los hallazgos locales.

## Qué hace cada herramienta

**Parquet es el formato de los archivos**, como CSV, pero conserva tipos y almacena por columnas con compresión. El reto exige Parquet o Delta. Aquí permite leer fechas y medidas sin cargar el JSON original completo.

**DuckDB es el motor SQL** que lee esos archivos y construye el modelo. Se usa dentro de Python, sin administrar un servidor de base de datos. Su función en esta parte es ejecutar SQL controlado por la aplicación. El SQL escrito por usuarios tiene otra frontera de seguridad, explicada en [SECURITY.md](SECURITY.md).

**SQLite guarda el estado operativo**: qué extracción y modelo están publicados, sus versiones y el historial. Los datos analíticos siguen en Parquet. Publicar ambos identificadores en una sola transacción evita una mezcla entre datos nuevos y modelo antiguo.

**SQLGlot pertenece al servicio de consultas de la Parte 3**. No interviene en las fórmulas de US-08 ni hace falta conocerlo para ejecutar los informes de esta parte. FastAPI, Pydantic, HTTPX y PyArrow no son dependencias directas de esta implementación.

## Demostración local

Desde `D:\Projects\Arkham`, con el entorno instalado según el README:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model status --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model verify --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model catalog --data-dir data --persona analyst
.\.venv\Scripts\python.exe -m outage_explorer model report --data-dir data --model-id e5721cad365d47beb0a8a322199a3138 --start 2026-09-01 --end 2026-09-30 --output work/part2-demo-report.json
```

El último comando fija el modelo conservado que sustenta [FINDINGS.md](../FINDINGS.md). Si se omite `--model-id`, analiza el modelo actualmente publicado. En un clon sin los datos locales, primero se debe ejecutar la ingestión descrita en el README; los identificadores serán diferentes.

`model verify` comprueba tipos, claves, relaciones, hashes y la métrica. `model report` ejecuta 18 consultas identificadas por nombre y hash. Las fechas se vinculan como parámetros para la reconciliación del intervalo; las consultas de anomalías históricas conservan sus fechas explícitas. El JSON incluye columnas, valores y procedencia; los decimales se representan como texto para conservar su precisión.

## Decisiones que debes poder defender

1. **Una tabla de hechos por grano.** Nacional, instalación y generador son observaciones independientes. Conservarlas permite detectar desacuerdos; sustituir una por la suma de otra ocultaría el problema que US-07 pide investigar.

2. **Identificadores como texto y claves compuestas.** Un generador `1` puede existir en varias instalaciones. Su identidad es `(facility_id, generator_id)`. Los nombres pueden cambiar y no sirven como claves. Capacidad es una medida diaria, porque el denominador cambia con el tiempo.

3. **Cero, ausencia y nulo tienen significados diferentes.** No se inventan observaciones ni se rellenan medidas con cero. Las reconciliaciones conservan padres o hijos ausentes y muestran los recuentos que permiten interpretar una suma. Un calendario auxiliar permite detectar fechas ausentes sin fabricar hechos.

4. **US-08 usa los operandos nacionales del mismo día.** La fracción es `outage_mw / capacity_mw`; el porcentaje multiplica por 100. Python `Decimal` aplica el redondeo documentado. Una capacidad nula o no positiva produce una métrica indefinida. No se promedian porcentajes de generadores.

5. **Advertir no significa corregir el origen.** Un dato extraño se conserva con evidencia y una regla de revisión mientras sus valores y resultados derivados sean representables. Las claves inválidas, la pérdida de precisión o un desbordamiento derivado bloquean la publicación completa y conservan la versión anterior; una anomalía semántica no se elimina silenciosamente.

6. **Versiones inmutables antes de optimizar.** Los archivos nuevos se validan antes de publicar. Las versiones previas siguen disponibles. Los archivos se ordenan por su clave primaria; en los hechos diarios, la fecha va primero. Con unos 46 MB de datos originales, dividirlos en muchas particiones pequeñas no aporta una ventaja demostrada. El diseño para mayores volúmenes y sus costes está en [DATA_MODEL.md](DATA_MODEL.md).

## Qué muestran los datos reales

Septiembre de 2026 tiene 30 días comparados, 1.650 instalación-días y 2.850 generador-días. Las capacidades y los MW fuera de servicio coinciden exactamente entre los tres granos. Los porcentajes publicados sí presentan diferencias reproducibles de redondeo. No se inventa un desacuerdo de MW para cumplir el enunciado.

Los tres hallazgos principales son Indian Point 2 con capacidad cero y porcentaje positivo; 120 días ausentes de River Bend; y el cambio de capacidad de Grand Gulf de 1.463 a 1.190 MW. Cada uno incluye fechas, identidades, valores, consultas y tratamiento en el producto. La coincidencia de las sumas no demuestra que todas las instalaciones estén presentes.

La matriz completa de requisitos y los resultados observados están en [PART2_ACCEPTANCE.md](../PART2_ACCEPTANCE.md). Las explicaciones de causa distinguen hechos demostrados de hipótesis que aún requieren información del equipo o del proveedor.
