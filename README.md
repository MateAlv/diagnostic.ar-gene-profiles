# diagnostic.ar-gene-profiles

La base de genes en español que usa [diagnostic.ar](https://github.com/MateAlv/diagnosticar-plataforma) para explicar cada gen del resultado: qué enfermedades se asocian a él, qué síntomas suelen aparecer y con qué frecuencia. Para los profesionales suma también la bioquímica, que a las familias no se les muestra.

Sale del catálogo de genes que armó Lucía Lourengo Caridade, cuyos scripts originales están en `legacy/`. Acá sumamos la descarga de los datos desde sus fuentes, así la base se puede reconstruir desde cero.

La base no se guarda en el repo. Se arma con los scripts de `genesdb/` y cada versión se publica en las Releases del repo. La que usa hoy la plataforma es **db-v1.0.0**. Los pasos para construirla están en [docs/BUILDING.md](docs/BUILDING.md), y cómo se escriben y revisan los resúmenes en lenguaje simple, en [docs/PLAIN_LANGUAGE.md](docs/PLAIN_LANGUAGE.md).

Es parte del Trabajo Profesional de Ingeniería en Informática (FIUBA) de Mateo Alvarez, Mariano Brizuela López, Gonzalo Bustamante y Lucía Lourengo Caridade.

## Probarlo

```bash
pip install -r requirements-dev.txt
pytest
```

Los tests no usan la red.

## Datos

Usa la ontología HPO y sus traducciones oficiales al español, NCBI Gene, UniProtKB (CC BY 4.0), datos de Orphanet (CC BY 4.0) que llegan a través de las anotaciones de HPO, y la tabla pública mim2gene de OMIM. Cada base guarda en su tabla `build_info` la versión de HPO y la fecha con que se armó.

## Licencia

GPL-3.0 para el código. La carpeta `legacy/` es el trabajo original de Lucía Lourengo Caridade y no está bajo esta licencia. Los datos siguen las licencias de sus fuentes.
