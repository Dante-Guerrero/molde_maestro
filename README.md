# molde_maestro

`molde_maestro` es un pipeline reproducible para **mejorar repositorios** usando:
- un **modelo razonador** (p. ej., `deepseek-r1`) que genera un plan de cambios,
- **Aider** para implementar ese plan en una rama dedicada,
- ejecución de **lint + tests** para validar,
- y un **reporte final** contrastando resultados contra objetivos del proyecto.

Todo queda auditado en artefactos dentro de `AI/` (plan, logs, reportes).

---

## Qué hace exactamente

Flujo (modo `run`):

1) (Opcional) **snapshot** del repo con `git archive` (zip sin `.git`)
2) El **razonador** genera `AI/plan.md` (plan estructurado de cambios)
3) Se crea una **rama** `ai/<timestamp>-improvements`
4) **Aider** implementa el plan (con auto-commits)
5) Se corren **lint** (opcional) y **tests**
6) Si fallan tests, reintenta hasta `max_iters` veces, pasando el reporte como feedback a Aider
7) El **razonador** genera `AI/final.md` con conclusiones vs objetivos

---

## Requisitos

### Obligatorios
- `python3`
- `git`
- repo inicializado como git

### Para usar modelos locales con Ollama
- `ollama` instalado y funcionando
- modelo razonador disponible (p. ej. `deepseek-r1`)

### Para aplicar cambios con Aider
- `aider` instalado y accesible en `PATH`
- un modelo coder configurado (ej: `ollama:qwen3-coder:14b`)

### YAML (opcional)
Si quieres usar config en YAML:
- `pip install pyyaml`

JSON no requiere dependencias.

---

## Conceptos clave

### `PROJECT_GOALS.md`
Archivo (en la raíz del repo, por defecto) con los **objetivos generales**. Es la referencia principal para evaluar resultados.

Ejemplo:
- No romper API pública
- Mejorar cobertura de tests
- Mejorar performance del módulo X

### Directorio `AI/`
Por defecto, el pipeline genera artefactos aquí:

- `AI/plan.md`  
  Plan generado por el razonador con cambios ordenados, criterios de aceptación y comandos de validación.
- `AI/aider-log.txt`  
  Log de ejecución de Aider.
- `AI/test-report.md` y `AI/test-report.json`  
  Resultados de lint/tests.
- `AI/final.md`  
  Informe final comparando cambios y evidencia vs objetivos.
- `AI/snapshots/<timestamp>.zip` (si `zip: true`)  
  Snapshot del repo.

---

## Archivo de configuración

Para no escribir 10 flags cada vez, `pipeline.py` busca automáticamente:

1) `--config <ruta>`
2) `molde_maestro.yml`
3) `molde_maestro.yaml`
4) `molde_maestro.json`

en la raíz del repo.

**Regla:** la CLI siempre pisa el config.

### Ejemplo `molde_maestro.yml`

```yaml
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

También soporta claves con guiones (`max-iters`) y las normaliza a guion bajo.

---

## Uso rápido

## Primeros pasos (en 5 minutos)

Este proyecto asume dos cosas:
1) Tienes un **repo objetivo** (el que quieres mejorar) que es un repo git real.
2) Tienes instalados los “obreros” del pipeline: **Ollama** (para el razonador) y **Aider** (para aplicar cambios).

### 0) Instala herramientas (una vez)

- **Ollama**: instala Ollama y descarga tus modelos (ej: `deepseek-r1` y `qwen3-coder:14b`).
- **Aider**: instala Aider y asegúrate de que el comando `aider` funcione en tu terminal.
- (Opcional) **PyYAML** si usarás config en YAML:
  ```bash
  pip install pyyaml
  ```

### 1) Prepara el repo objetivo

En la raíz del repo objetivo, crea/edita:

- `PROJECT_GOALS.md` (obligatorio, si quieres que el reporte final tenga sentido)

Ejemplo mínimo:
- No romper API pública
- Mejorar cobertura de tests
- Reducir errores en módulo X

### 2) Crea la configuración del pipeline

En este repo (`molde_maestro/`), crea `molde_maestro.yml` (o `.json`) y apunta `repo:` al repo objetivo.

Ejemplo si ambos repos viven en la misma carpeta padre:

```yaml
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

### 3) Ejecuta

Desde la raíz de `molde_maestro/`:

```bash
python pipeline.py
```

Si existe `molde_maestro.yml/.yaml/.json`, el script asume `run` automáticamente.

### 4) Revisa resultados (en el repo objetivo)

El pipeline escribe todo dentro del repo objetivo:

- `AI/plan.md` (plan de cambios)
- `AI/test-report.md` (salida de tests/lint)
- `AI/final.md` (conclusiones vs objetivos)

y crea una rama tipo `ai/<timestamp>-improvements` en el repo objetivo. Revisa el diff y los reportes antes de mergear.

---


## Ejecutar el pipeline sobre OTRO repositorio (caso típico)

Puedes mantener este repo (`molde_maestro/`) como “motor” y ejecutar el pipeline sobre un repo objetivo distinto (por ejemplo `exam_pipeline_starter/`).

### Estructura recomendada de carpetas

Asumiendo que ambos repos viven en la misma carpeta padre:

```text
<carpeta_padre>/
├─ molde_maestro/
│  ├─ pipeline.py
│  ├─ molde_maestro.yml
│  └─ README.md
│
└─ exam_pipeline_starter/
   ├─ .git/
   ├─ PROJECT_GOALS.md
   ├─ README.md
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

### Cómo configurar `molde_maestro.yml` para apuntar al repo objetivo

En `molde_maestro/molde_maestro.yml`, ajusta `repo:` para que apunte al repo objetivo.  
En el ejemplo de arriba, desde `molde_maestro/` el repo objetivo queda en `../exam_pipeline_starter`:

```yaml
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

**Importante:** `goals:` se resuelve dentro del repo objetivo. Es decir, este ejemplo espera que exista:
- `exam_pipeline_starter/PROJECT_GOALS.md`

### Paso a paso (ejecución real)

1) Crea/edita `PROJECT_GOALS.md` en el repo objetivo (`exam_pipeline_starter/`).
2) En `molde_maestro.yml`, apunta `repo:` al repo objetivo (ej: `../exam_pipeline_starter`).
3) Ejecuta desde `molde_maestro/`:

```bash
cd /ruta/a/molde_maestro
python pipeline.py
```

4) Revisa artefactos y resultados en el repo objetivo:
- `exam_pipeline_starter/AI/plan.md`
- `exam_pipeline_starter/AI/test-report.md`
- `exam_pipeline_starter/AI/final.md`

Y revisa la rama creada en el repo objetivo (ej: `ai/<timestamp>-improvements`) antes de mergear.

---


### 1) Crea tus objetivos
En la raíz del repo:
- `PROJECT_GOALS.md`

### 2) Crea el config
- `molde_maestro.yml` (recomendado) o `molde_maestro.json`

### 3) Ejecuta
Desde la raíz del repo:

```bash
python pipeline.py
```

Si hay config, `pipeline.py` asume el subcomando `run` automáticamente.

---

## Subcomandos

> Nota: Si usas config, puedes omitir varios parámetros. Si no usas config, tendrás que pasarlos por CLI.

### `run` (todo el flujo)
Ejemplo completo:

```bash
python pipeline.py run   --repo .   --goals PROJECT_GOALS.md   --reasoner ollama:deepseek-r1   --aider-model ollama:qwen3-coder:14b   --test-cmd "pytest -q"   --lint-cmd "ruff check . || true"   --max-iters 2   --zip
```

### `plan` (solo generar plan)
Genera `AI/plan.md`:

```bash
python pipeline.py plan --reasoner ollama:deepseek-r1
```

### `apply` (solo implementar plan con Aider)
Requiere que exista `AI/plan.md`:

```bash
python pipeline.py apply --aider-model ollama:qwen3-coder:14b
```

Opciones útiles:
- `--branch "ai/mi-rama"`
- `--base main`
- `--allowed-file src/app.py` (repeatable, restringe archivos a tocar)
- `--aider-extra-arg ...` (para flags extra del CLI de aider)

### `test` (solo lint + tests)
Genera `AI/test-report.*`:

```bash
python pipeline.py test --test-cmd "pytest -q" --lint-cmd "ruff check . || true"
```

### `report` (solo reporte final)
Requiere `AI/plan.md` y preferiblemente `AI/test-report.md`:

```bash
python pipeline.py report --reasoner ollama:deepseek-r1
```

Opcional:
- `--base-ref <ref>` para controlar el diff usado como evidencia.

### `snapshot` (zip del repo)
Crea un zip reproducible con `git archive`:

```bash
python pipeline.py snapshot --zip
```

---

## Cómo decide el “base diff” para el reporte

Para comparar cambios, el pipeline intenta:
1) `merge-base` con el upstream (`@{u}`) si existe
2) si no, usa `HEAD~1`

Luego hace:

- `git diff <base_ref>...HEAD`

Esto alimenta el reporte `AI/final.md`.

---

## Personalización común

### Cambiar comandos según stack

**Node**
```yaml
test_cmd: "npm test"
lint_cmd: "npm run lint || true"
```

**Go**
```yaml
test_cmd: "go test ./..."
```

**Rust**
```yaml
test_cmd: "cargo test"
```

### Repos grandes
El razonador recibe:
- `git ls-files` truncado a `max_tree_lines`
- algunos archivos clave (README, package/pyproject, workflows, etc.)
- `extra_context` para forzar incluir archivos relevantes

Ejemplo:
```yaml
extra_context:
  - src/core.py
  - docs/architecture.md
```

---

## Troubleshooting

### “Config YAML encontrado pero falta PyYAML”
Instala:
```bash
pip install pyyaml
```
O usa JSON.

### Aider no reconoce `--message`
Tu versión de Aider cambió el CLI. Solución rápida:
- corre `aider --help`
- ajusta `run_aider()` en `pipeline.py` para usar el flag equivalente (manteniendo `--auto-commits` si existe).

### El razonador inventa comandos de test/lint
Ponlos explícitos en config (recomendado). El plan igual puede sugerir, pero tu pipeline ejecuta lo que tú defines.

---

## Seguridad y buenas prácticas

- Trabaja siempre en una **rama** generada por el pipeline.
- Revisa `AI/plan.md` antes de aplicar en proyectos críticos.
- Revisa el diff (`git diff`) y el reporte `AI/final.md` antes de mergear.


