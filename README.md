# molde_maestro

`molde_maestro` es un CLI para planear, aplicar, validar y reportar mejoras sobre repositorios Git reales. Su objetivo no es solo editar código: deja una corrida reproducible y auditable con artefactos en `AI/` para revisar qué se propuso, qué se cambió y cómo se validó.

## Qué hace

El flujo general del proyecto es:

1. leer objetivos desde `PROJECT_GOALS.md` o `--goals-text`
2. generar un plan con un modelo `reasoner`
3. aplicar el plan con `aider`
4. validar el resultado con tests, lint y chequeos semánticos cuando correspondan
5. generar un reporte final con evidencia real del diff, la rama usada y la validación ejecutada

Subcomandos principales:

- `plan`: genera `AI/plan.md`
- `apply`: aplica el plan con Aider
- `test`: ejecuta validaciones y genera `AI/test-report.md`
- `report`: genera `AI/final.md`
- `run`: ejecuta el pipeline completo
- `snapshot`: crea un zip del repo en un ref dado

## Flujo de uso recomendado

La arquitectura de uso recomendada separa tres cosas:

1. el repositorio fuente de `molde_maestro`
2. una instalación aislada del CLI
3. uno o varios repositorios objetivo sobre los que se ejecuta el comando

Eso significa que `molde_maestro` no debe instalarse dentro de cada repo objetivo ni tratarse como dependencia del proyecto que va a modificar. Conviene usarlo como herramienta externa.

Ejemplo de flujo:

```text
~/github/molde_maestro          # repo fuente del CLI
~/tools/uv-tools/...            # instalación aislada del comando
~/github/proyecto_objetivo_a    # repo objetivo
~/github/proyecto_objetivo_b    # otro repo objetivo
```

Uso esperado sobre un repo objetivo real:

```bash
cd ~/github/proyecto_objetivo
molde-maestro run
```

El comando se ejecuta desde el repo objetivo, pero la herramienta debe estar instalada aparte.

## Requisitos

- Python 3.11+
- `git`
- `uv`
- `aider`
- `ollama`
- al menos un modelo disponible en Ollama para `reasoner` y `aider_model`

## Instalación

### Instalación recomendada para uso diario

Para uso diario, instala `molde_maestro` como CLI aislado. La opción principal recomendada es `uv tool install`. Como alternativa válida, puedes usar `pipx install`.

Esta forma de instalación es la correcta porque:

- aísla las dependencias del CLI
- evita contaminar los entornos de los repos objetivo
- deja el comando disponible globalmente
- facilita desinstalar o reemplazar la versión instalada
- separa claramente el uso operativo del desarrollo del paquete

Flujo recomendado desde el repo fuente de `molde_maestro`:

```bash
uv build
uv tool install dist/molde_maestro-0.1.0-py3-none-any.whl
```

Alternativa con `pipx`:

```bash
uv build
pipx install dist/molde_maestro-0.1.0-py3-none-any.whl
```

Notas:

- el wheel generado queda en `dist/`
- para una instalación estable y repetible conviene instalar una versión construida
- aunque también puedes instalar desde el árbol fuente, para uso diario es mejor no atar el comando al estado cambiante del repositorio

### Instalación de desarrollo

La instalación editable o de desarrollo sí corresponde cuando estás trabajando en `molde_maestro` mismo.

Ejemplos válidos:

```bash
uv sync --extra dev
```

```bash
pip install -e .
```

```bash
uv run molde-maestro --help
```

Esto es útil para desarrollar, probar y depurar el CLI, pero no debería ser la instalación principal de uso diario porque:

- el comando queda atado al estado actual del repo fuente
- cualquier cambio local puede alterar el comportamiento del CLI
- sirve para iterar sobre el paquete, no para una instalación estable

## Uso básico

Ayuda general:

```bash
molde-maestro --help
```

Ayuda de un subcomando:

```bash
molde-maestro run --help
```

Si existe `molde_maestro.yml`, `molde_maestro.yaml` o `molde_maestro.json` en el directorio actual, la CLI puede tomar esa configuración y, si invocas `molde-maestro` sin subcomando, asume `run`.

Ejemplo mínimo con archivo de configuración:

```bash
molde-maestro run
```

Ejemplo equivalente indicando el archivo:

```bash
molde-maestro --config ./molde_maestro.yml run
```

También puedes ejecutar todo por flags:

```bash
molde-maestro run \
  --repo . \
  --goals PROJECT_GOALS.md \
  --reasoner ollama:qwen2.5-coder:7b \
  --aider-model ollama:qwen2.5-coder:7b \
  --test-cmd "python3 -m compileall src"
```

## Uso sobre repositorios objetivo

La herramienta está pensada para correr dentro del repositorio objetivo, no para instalarse dentro de él.

Ejemplo práctico:

```bash
cd ~/github/proyecto_objetivo
cp ~/github/molde_maestro/molde_maestro.yml .
molde-maestro run
```

Puntos importantes:

- `--repo` apunta al repositorio que será inspeccionado y modificado
- las rutas relativas del config se resuelven respecto al archivo de configuración
- `run` y `apply` detectan la rama original y, por defecto, trabajan sobre una rama aislada `ai/<timestamp>-<slug>`
- al terminar, la herramienta te deja en la rama resultado para revisar cambios y artefactos

Artefactos y salidas habituales:

- `AI/plan.md`
- `AI/test-report.md`
- `AI/final.md`
- `AI/snapshots/*.zip` si activas `--zip`

## Precauciones operativas

`molde_maestro` trabaja sobre repos Git reales, puede crear ramas, modificar archivos, ejecutar validaciones y dejar reportes de la corrida. Antes de usarlo sobre proyectos importantes, conviene seguir estas precauciones:

- preferir un `git status` limpio antes de `run` o `apply`
- usar repos con remoto o algún backup recuperable
- revisar siempre el diff, la rama generada y los reportes en `AI/`
- empezar con `plan`, `test` o configuraciones conservadoras antes de automatizar flujos completos
- usar `--allow-dirty-repo` solo si entiendes el riesgo de mezclar cambios previos con la corrida
- no usar `--unsafe-shell` salvo que entiendas exactamente su impacto

Detalles operativos reales del proyecto:

- en modo no interactivo, `run` y `apply` fallan si el repo está sucio, salvo que actives `--allow-dirty-repo`
- en sesiones interactivas, la CLI puede pedir datos faltantes y resolver algunas decisiones manuales
- `test_cmd`, `lint_cmd` con metacaracteres de shell y `reasoner=cmd:...` requieren `--unsafe-shell`
- si la solución introduce dependencias nuevas, `molde_maestro` no las agrega silenciosamente: en modo interactivo pide aprobación y en no interactivo deja la resolución pendiente

## Actualización y mantenimiento

Si usas `molde_maestro` como herramienta aislada, el mantenimiento es simple: reconstruyes una nueva versión del wheel y reemplazas la instalación anterior con esa versión.

Para verificar que el comando instalado sigue disponible:

```bash
molde-maestro --help
```

Para desarrollo del propio proyecto, la CI actual:

- ejecuta `pytest`
- verifica higiene de archivos trackeados
- construye wheel y sdist con `uv build`
- instala el wheel generado y valida `molde-maestro --help`

## Referencia rápida

Pipeline completo:

```bash
molde-maestro run
```

Generar solo el plan:

```bash
molde-maestro plan --repo .
```

Aplicar un plan existente:

```bash
molde-maestro apply --repo .
```

Ejecutar validaciones:

```bash
molde-maestro test --repo . --test-cmd "python3 -m compileall src"
```

Generar solo el reporte:

```bash
molde-maestro report --repo .
```

Crear un snapshot zip:

```bash
molde-maestro snapshot --repo . --zip
```
