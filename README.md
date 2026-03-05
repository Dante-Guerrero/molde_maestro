# molde_maestro

`molde_maestro` es un pipeline reproducible para **mejorar repositorios** usando:

- un **modelo razonador** (por ejemplo vía `ollama`) que genera un plan de cambios,
- **Aider** para implementar ese plan en una rama dedicada,
- ejecución de **lint + tests** para validar,
- y un **reporte final** que contrasta resultados contra objetivos del proyecto.

Todo queda auditado en artefactos dentro de `AI/` (plan, logs, reportes).

---

## Qué es (y qué no es)

- **Es** un “motor” que corre sobre un **repo objetivo** (otro repo), genera un plan, crea una rama, aplica cambios con Aider y valida.
- **No es** un plugin mágico de GitHub. Es un CLI que tú ejecutas localmente (o en CI si quieres sufrir profesionalmente).

El comando principal es:

- `molde-maestro ...`

(Se instala desde este repo porque está empaquetado como proyecto Python con `pyproject.toml` y entrypoint).

---

## Flujo: qué hace exactamente

Flujo completo (subcomando `run`):

1) (Opcional) **snapshot** del repo con `git archive` (zip sin `.git`)
2) El **razonador** genera `AI/plan.md` (plan estructurado de cambios)
3) Se crea una **rama** `ai/<timestamp>-improvements`
4) **Aider** implementa el plan (con auto-commits)
5) Se corre **lint** (opcional) y **tests**
6) Si fallan tests, reintenta hasta `max_iters` veces, usando el reporte como feedback a Aider
7) El **razonador** genera `AI/final.md` (conclusiones vs objetivos + evidencia)

---

## Requisitos

### Obligatorios
- Python 3.11+ (recomendado)
- `git`
- El repo objetivo debe ser un repo git real

### Para usar modelos locales con Ollama
- `ollama` instalado y funcionando
- un modelo razonador disponible (ej: `deepseek-r1`)

### Para aplicar cambios con Aider
- `aider` instalado y accesible en tu `PATH`
- un modelo “coder” configurado para Aider (ej: `ollama:qwen3-coder:14b`)

> Nota: `molde_maestro` ejecuta `aider` como proceso externo. Si `aider` no existe en tu terminal, no hay magia que lo arregle.

---

## Instalación con `uv` (recomendado)

### 1) Instala `uv`
Si aún no lo tienes, instala `uv` siguiendo el método oficial de tu sistema (macOS, Linux o Windows).  
(En macOS suele ser por `brew install uv`, y en otros sistemas hay instalador).

### 2) Crea el entorno e instala `molde_maestro`
Desde la raíz de este repo (`molde_maestro/`):

```bash id="pxwtp5"
uv sync
```

Esto:
- crea/usa un entorno virtual (gestionado por `uv`)
- instala dependencias del proyecto (según `pyproject.toml` y `uv.lock`)
- deja disponible el comando `molde-maestro` dentro del entorno

### 3) Ejecuta el CLI dentro del entorno
Opción A (simple): prefix con `uv run`:

```bash id="17twmd"
uv run molde-maestro --help
```

Opción B (si prefieres activar el venv que `uv` creó): usa el venv de `.venv/` (si aplica en tu setup) y ejecuta `molde-maestro` normalmente.

> Consejo práctico: usa `uv run ...` y listo. Menos ceremonias, menos drama.

---

## Conceptos clave

### `PROJECT_GOALS.md`
Archivo (en la raíz del repo objetivo, por defecto) con los **objetivos generales**. Es la referencia principal para evaluar resultados.

Ejemplo:
- No romper API pública
- Mejorar cobertura de tests
- Reducir errores en el módulo X

### Directorio `AI/`
Por defecto el pipeline genera artefactos en el repo objetivo:

- `AI/plan.md`  
  Plan del razonador: cambios ordenados, criterios de aceptación y comandos de validación.
- `AI/aider-log.txt`  
  Log de ejecución de Aider.
- `AI/test-report.md` y `AI/test-report.json`  
  Resultados de lint/tests.
- `AI/final.md`  
  Informe final: objetivos vs evidencia (diff + reportes).
- `AI/snapshots/<timestamp>.zip` (si `zip: true`)  
  Snapshot reproducible del repo (vía `git archive`).

---

## Archivo de configuración

Para no pasar 10 flags siempre, `molde_maestro` busca config automáticamente en la **raíz del repo desde donde ejecutas**:

1) `--config <ruta>`
2) `molde_maestro.yml`
3) `molde_maestro.yaml`
4) `molde_maestro.json`

**Regla:** la CLI siempre pisa el config.

### Ejemplo `molde_maestro.yml`

```yaml id="hbvkux"
repo: .
goals: PROJECT_GOALS.md
ai_dir: AI

reasoner: "ollama:deepseek-r1"
aider_model: "ollama:qwen3-coder:14b"

test_cmd: "pytest -q"
lint_cmd: "ruff check . || true"

max_iters: 2
zip: true
```

También se aceptan claves con guiones (ej: `max-iters`) y se normalizan a guion bajo (`max_iters`).

---

## Primeros pasos (en 5 minutos)

Este proyecto asume:
1) Tienes un **repo objetivo** (el que quieres mejorar) y es un repo git real.
2) Tienes instalados los “obreros”: **Ollama** (razonador) y **Aider** (implementación).

### 0) Instala herramientas (una vez)
- Instala **Ollama**, y descarga tus modelos (ej: `deepseek-r1` y `qwen3-coder:14b`).
- Instala **Aider**, y verifica que esto funcione:

```bash id="eya72k"
aider --version
```

### 1) Prepara el repo objetivo
En la raíz del repo objetivo crea/edita:

- `PROJECT_GOALS.md`

Ejemplo mínimo:
- No romper API pública
- Mejorar cobertura de tests
- Reducir errores en módulo X

### 2) Define la configuración del pipeline
Crea `molde_maestro.yml` en **este repo** (`molde_maestro/`) o donde prefieras ejecutarlo, y apunta `repo:` al repo objetivo.

Ejemplo si ambos repos viven en la misma carpeta padre:

```yaml id="wnwpvu"
repo: ../exam_pipeline_starter
goals: PROJECT_GOALS.md
ai_dir: AI

reasoner: "ollama:deepseek-r1"
aider_model: "ollama:qwen3-coder:14b"

test_cmd: "pytest -q"
lint_cmd: "ruff check . || true"

max_iters: 2
zip: true
```

**Importante:** `goals:` se resuelve dentro del repo objetivo.  
Este ejemplo espera que exista: `exam_pipeline_starter/PROJECT_GOALS.md`.

### 3) Ejecuta
Desde la raíz de `molde_maestro/`:

```bash id="cwvyxf"
uv run molde-maestro run
```

Si existe `molde_maestro.yml/.yaml/.json`, el CLI tomará valores de ahí y solo necesitas `run`.

### 4) Revisa resultados (en el repo objetivo)
En el repo objetivo verás:

- `AI/plan.md`
- `AI/test-report.md` (+ `.json`)
- `AI/final.md`

Y una rama tipo `ai/<timestamp>-improvements`. Revisa el diff y los reportes antes de mergear.

---

## Ejecutar el pipeline sobre OTRO repositorio (caso típico)

Puedes mantener este repo (`molde_maestro/`) como “motor” y ejecutar el pipeline sobre otro repo objetivo (por ejemplo `exam_pipeline_starter/`).

### Estructura recomendada

```text id="akqahc"
<carpeta_padre>/
├─ molde_maestro/
│  ├─ pyproject.toml
│  ├─ uv.lock
│  ├─ molde_maestro.yml
│  └─ src/molde_maestro/pipeline.py
│
└─ exam_pipeline_starter/
   ├─ .git/
   ├─ PROJECT_GOALS.md
   ├─ pyproject.toml          (o requirements.txt / package.json, según stack)
   ├─ src/                    (o app/, etc.)
   ├─ tests/                  (si existe)
   └─ AI/                     (se crea cuando corras el pipeline)
      ├─ plan.md
      ├─ aider-log.txt
      ├─ test-report.md
      ├─ test-report.json
      ├─ final.md
      └─ snapshots/
         └─ 20260305-123456.zip   (si zip=true)
```

### Paso a paso (real)
1) En `exam_pipeline_starter/`, crea/edita `PROJECT_GOALS.md`.
2) En `molde_maestro/molde_maestro.yml`, configura `repo: ../exam_pipeline_starter`.
3) Ejecuta:

```bash id="zqcrpt"
cd /ruta/a/molde_maestro
uv run molde-maestro run
```

4) Revisa artefactos en:
- `exam_pipeline_starter/AI/plan.md`
- `exam_pipeline_starter/AI/test-report.md`
- `exam_pipeline_starter/AI/final.md`

---

## Subcomandos útiles

> Si usas config, puedes omitir muchos parámetros. Si no, pásalos por CLI.

### `run` (todo el flujo)
Ejemplo “todo por CLI”:

```bash id="kzouia"
uv run molde-maestro run   --repo .   --goals PROJECT_GOALS.md   --reasoner ollama:deepseek-r1   --aider-model ollama:qwen3-coder:14b   --test-cmd "pytest -q"   --lint-cmd "ruff check . || true"   --max-iters 2   --zip
```

### `plan` (solo generar plan)
```bash id="tjuo7s"
uv run molde-maestro plan --reasoner ollama:deepseek-r1
```

### `apply` (solo implementar plan con Aider)
Requiere `AI/plan.md`:

```bash id="359znz"
uv run molde-maestro apply --aider-model ollama:qwen3-coder:14b
```

Opciones útiles:
- `--branch "ai/mi-rama"`
- `--base main`
- `--allowed-file src/app.py` (repetible; restringe qué archivos puede tocar)
- `--aider-extra-arg ...` (para pasar flags extra al CLI de Aider)

### `test` (solo lint + tests)
Genera `AI/test-report.*`:

```bash id="a8d3a7"
uv run molde-maestro test   --test-cmd "pytest -q"   --lint-cmd "ruff check . || true"
```

### `report` (solo reporte final)
Requiere `AI/plan.md` y preferiblemente `AI/test-report.md`:

```bash id="mphq12"
uv run molde-maestro report --reasoner ollama:deepseek-r1
```

Opcional:
- `--base-ref <ref>` para controlar el diff usado como evidencia.

### `snapshot` (zip del repo)
```bash id="a842ob"
uv run molde-maestro snapshot --zip
```

---

## Cómo decide el “base diff” para el reporte

Para comparar cambios, el pipeline intenta:
1) `merge-base` con el upstream (`@{u}`) si existe
2) si no, usa `HEAD~1`

Luego usa:

- `git diff <base_ref>...HEAD`

Eso alimenta el reporte `AI/final.md`.

---

## Personalización común

### Cambiar comandos según stack

**Node**
```yaml id="uncwxb"
test_cmd: "npm test"
lint_cmd: "npm run lint || true"
```

**Go**
```yaml id="869dnd"
test_cmd: "go test ./..."
```

**Rust**
```yaml id="h5af7d"
test_cmd: "cargo test"
```

### Repos grandes
Puedes forzar contexto adicional para el razonador:

```yaml id="jcorjd"
extra_context:
  - src/core.py
  - docs/architecture.md
```

---

## Troubleshooting

### “No se encuentra `aider`”
Instala Aider y asegúrate de que el comando exista en tu shell:

```bash id="fu990j"
aider --version
```

### El razonador inventa comandos de test/lint
Ponlos explícitos en config. El plan puede sugerir cosas, pero el pipeline ejecuta lo que tú defines.

### Aider cambió flags (CLI mismatch)
Revisa:

```bash id="4ndxct"
aider --help
```

Y ajusta la configuración/args extra (`--aider-extra-arg`) según lo que soporte tu versión.

---

## Buenas prácticas (para que no te odies mañana)

- Trabaja siempre en la **rama** generada por el pipeline.
- Revisa `AI/plan.md` antes de aplicar en proyectos críticos.
- Revisa diff (`git diff`) y `AI/final.md` antes de mergear.
