# molde_maestro

## 1. Función principal del repositorio

`molde_maestro` es un pipeline CLI para automatizar mejoras sobre repositorios Git con este flujo:

1. leer objetivos desde `PROJECT_GOALS.md`
2. generar un plan con un modelo reasoner
3. aplicar cambios con `aider`
4. validar el resultado con tests, lint y chequeos semánticos
5. generar un reporte final con evidencia real del diff y de la validación

El objetivo del proyecto no es solo “editar código”, sino dejar una corrida reproducible, auditable y con artefactos en `AI/` para entender qué se planeó, qué se cambió y cómo se validó.

## 2. Formas de ejecutar `molde_maestro`

### Requisitos

- Python 3.11+
- `uv`
- `git`
- `aider`
- `ollama`
- al menos un modelo disponible en Ollama para:
  - `reasoner`
  - `aider_model`

### Instalación

Desde la raíz del repo:

```bash
uv sync --extra dev
```

Si quieres ejecutar el CLI sin instalarlo globalmente:

```bash
uv run molde-maestro --help
```

Si prefieres invocarlo como módulo Python:

```bash
python -m molde_maestro.cli --help
```

### Formas de invocación

`molde_maestro` puede ejecutarse de cuatro maneras principales:

- como binario expuesto por el paquete: `molde-maestro ...`
- con `uv run`: `uv run molde-maestro ...`
- como módulo Python: `python -m molde_maestro.cli ...`
- con configuración, flags o modo interactivo según el caso

#### 1. Mostrar ayuda de la CLI

Para ver subcomandos y flags disponibles:

```bash
uv run molde-maestro --help
```

Ayuda de un subcomando concreto:

```bash
uv run molde-maestro run --help
uv run molde-maestro plan --help
uv run molde-maestro apply --help
uv run molde-maestro test --help
uv run molde-maestro report --help
uv run molde-maestro snapshot --help
```

#### 2. Ejecutar con archivo de configuración

Si existe `molde_maestro.yml`, `molde_maestro.yaml` o `molde_maestro.json` en el directorio actual, puedes correr:

```bash
uv run molde-maestro run
```

También puedes apuntar a un archivo específico:

```bash
uv run molde-maestro --config ./molde_maestro.yml run
```

Si ejecutas `uv run molde-maestro` sin subcomando y existe config, el programa asume `run`.

#### 3. Ejecutar pasando todo por flags

Puedes correr cualquier subcomando sin config, solo con argumentos CLI:

```bash
uv run molde-maestro run \
  --repo . \
  --goals PROJECT_GOALS.md \
  --reasoner ollama:qwen2.5-coder:7b \
  --aider-model ollama:qwen2.5-coder:7b \
  --test-cmd "python3 -m compileall src"
```

Esto mismo aplica para `plan`, `apply`, `test`, `report` y `snapshot`.

#### 4. Ejecutar en modo interactivo

Si la sesión es interactiva (`stdin` TTY), la CLI puede pedir datos faltantes en tiempo de ejecución:

- selección de `reasoner`
- selección de `aider_model`
- ingreso manual de goals si falta `PROJECT_GOALS.md`
- confirmaciones para limpiar artefactos generados
- confirmaciones para crear commits
- confirmaciones para agregar dependencias faltantes detectadas durante `apply`

Si la sesión no es interactiva, `molde_maestro` no espera input y falla de forma explícita cuando una decisión humana es necesaria.

### Subcomandos disponibles

#### `run`

Ejecuta el pipeline completo: plan -> apply -> test -> report.

```bash
uv run molde-maestro run
```

#### `plan`

Genera `AI/plan.md` usando el modelo reasoner.

```bash
uv run molde-maestro plan --repo .
```

#### `apply`

Aplica `AI/plan.md` con `aider`.

```bash
uv run molde-maestro apply --repo .
```

#### `test`

Ejecuta validación y genera `AI/test-report.md`.

```bash
uv run molde-maestro test --repo . --test-cmd "python3 -m compileall src"
```

#### `report`

Genera `AI/final.md` usando el plan, el diff y la validación real.

```bash
uv run molde-maestro report --repo .
```

#### `snapshot`

Crea un zip del repositorio en el ref indicado.

```bash
uv run molde-maestro snapshot --repo . --zip
```

### Configuración por archivo o por flags

Puedes combinar config + flags:

- el config define defaults
- los flags completan o sobreescriben lo necesario según el merge actual de la CLI
- las rutas relativas de `repo` dentro del config se resuelven respecto al directorio del archivo de config

Ejemplo:

```bash
uv run molde-maestro --config ./molde_maestro.yml run --repo ../otro_repo
```

### Ejemplos típicos de ejecución

Pipeline completo usando config local:

```bash
uv run molde-maestro run
```

Pipeline completo por flags:

```bash
uv run molde-maestro run \
  --repo . \
  --goals PROJECT_GOALS.md \
  --reasoner ollama:qwen2.5-coder:7b \
  --aider-model ollama:qwen2.5-coder:7b \
  --test-cmd "python3 -m compileall src"
```

Generar solo el plan:

```bash
uv run molde-maestro plan --repo .
```

Aplicar un plan existente:

```bash
uv run molde-maestro apply --repo .
```

Validar cambios:

```bash
uv run molde-maestro test --repo . --test-cmd "python3 -m compileall src"
```

Generar solo el reporte:

```bash
uv run molde-maestro report --repo .
```

Crear snapshot:

```bash
uv run molde-maestro snapshot --repo . --zip
```

## 3. Cómo usarlo paso a paso

### Preparar el repo objetivo

En el repo que quieres mejorar necesitas, como mínimo:

1. que sea un repo Git
2. que tenga un archivo `PROJECT_GOALS.md`
3. que tenga un comando de validación razonable o que el pipeline pueda inferir uno

Ejemplo mínimo de `PROJECT_GOALS.md`:

```md
# PROJECT_GOALS

- Corrige el bug de autenticación en `src/app/auth.py`.
- No agregues dependencias nuevas.
- Mantén los cambios acotados.
```

### Configuración

Puedes pasar todo por flags o crear un archivo `molde_maestro.yml`.

Ejemplo:

```yaml
repo: .
goals: PROJECT_GOALS.md
ai_dir: AI

reasoner: "ollama:qwen2.5-coder:7b"
aider_model: "ollama:qwen2.5-coder:7b"

plan_mode: fast
reasoner_timeout: 120
report_timeout: 120
aider_timeout: 600
test_timeout: 120

validation_profile: auto
semantic_validation: true
semantic_validation_mode: auto
semantic_validation_strict: true

test_cmd: "python3 -m compileall src"
lint_cmd: ""

max_iters: 1
zip: false
apply_max_plan_changes: 1
apply_limit_to_plan_files: true
apply_enforce_plan_scope: true
```

### Ejecutar el pipeline completo

```bash
uv run molde-maestro run
```

Si el repo objetivo contiene artefactos generados típicos como `.DS_Store`, `__pycache__/` o `*.pyc`, `molde-maestro` los detecta antes de aplicar cambios:

- si son archivos no trackeados, pregunta si quieres limpiarlos antes de evaluar si el repo está dirty;
- si son archivos trackeados, pregunta si quieres removerlos en la rama AI antes de invocar Aider;
- si respondes `n`, no limpia nada;
- si la sesión no es interactiva y hay limpieza necesaria, la corrida se detiene para no decidir por ti.

### Ejecutar subcomandos por separado

`apply` usa la misma detección de artefactos generados y también confirma con `y/n` antes de limpiar archivos trackeados que puedan degradar el contexto de Aider.

### Qué produce una corrida

Dentro del repo objetivo se crea `AI/` con artefactos como:

- `plan.md`
- `test-report.md`
- `final.md`
- `run-metadata.json`
- prompts y respuestas crudas del modelo
- logs de `aider`

### Cómo validar este mismo repositorio

Tests:

```bash
python3 -m unittest discover -s tests -q
```

Opcional con `pytest`:

```bash
uv run pytest -q
```

Lint:

```bash
uv run ruff check .
```

## 4. Explicación del contenido de todos los archivos

### Archivos de raíz

- `README.md`: documentación principal del proyecto.
- `LICENSE`: licencia del repositorio.
- `pyproject.toml`: metadatos del paquete, dependencias, extras de desarrollo y entrypoint `molde-maestro`.
- `uv.lock`: lockfile de dependencias para instalaciones reproducibles con `uv`.
- `molde_maestro.yml`: ejemplo o configuración local para correr el pipeline sin repetir flags.

### Código fuente en `src/molde_maestro/`

- `src/molde_maestro/__init__.py`: marca el paquete Python `molde_maestro`.
- `src/molde_maestro/cli.py`: frontera principal de la CLI; define parser, carga de config, merge de flags y dispatch de comandos.
- `src/molde_maestro/pipeline.py`: capa de compatibilidad y orquestación compartida; conserva wrappers y helpers centrales usados por comandos y tests.
- `src/molde_maestro/command_config.py`: dataclasses tipadas para normalizar argumentos por subcomando.
- `src/molde_maestro/utils.py`: utilidades generales de IO, ejecución de comandos, timestamps, lectura/escritura segura y helpers comunes.
- `src/molde_maestro/git_ops.py`: helpers Git para refs, diffs, ramas, snapshots y detección de cambios.
- `src/molde_maestro/models.py`: integración con modelos y `aider`; prompts, validación de modelos, llamadas a Ollama y preparación del repo para edición.
- `src/molde_maestro/validation.py`: lógica de validación automática, detección de contexto Python, smoke imports, auditoría de dependencias y generación de `test-report`.
- `src/molde_maestro/reporting.py`: recorder de corridas, metadata, manejo de stages y helpers de reporte/errores.

### Subcomandos en `src/molde_maestro/commands/`

- `src/molde_maestro/commands/__init__.py`: exporta los subcomandos disponibles.
- `src/molde_maestro/commands/plan.py`: implementa `molde-maestro plan`.
- `src/molde_maestro/commands/apply.py`: implementa `molde-maestro apply`.
- `src/molde_maestro/commands/test.py`: implementa `molde-maestro test`.
- `src/molde_maestro/commands/report.py`: implementa `molde-maestro report`.
- `src/molde_maestro/commands/run.py`: implementa `molde-maestro run`, que orquesta el flujo completo.
- `src/molde_maestro/commands/snapshot.py`: implementa `molde-maestro snapshot`.

### Tests

- `tests/test_pipeline.py`: suite unitaria principal; cubre config, parser, validación, reportes, comandos y regresiones del pipeline.

## Resumen rápido

Si quieres usar el proyecto con lo mínimo:

1. instala dependencias con `uv sync --extra dev`
2. prepara un repo Git con `PROJECT_GOALS.md`
3. configura `reasoner`, `aider_model` y `test_cmd`
4. ejecuta `uv run molde-maestro run`
5. revisa `AI/plan.md`, `AI/test-report.md` y `AI/final.md`
