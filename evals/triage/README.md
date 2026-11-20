# Examen de "triaje" (etapa 8, opción 2)

Pregunta que el examen mide: **¿la IA distingue un hallazgo de Vulture que
es código muerto de uno que es un falso positivo?** (sin ver la cobertura).

## Archivo `examen.jsonl`

Un caso por línea (JSON). GENERADO por `scripts/build_triage_exam.py` a
partir de los análisis de nivel 2: no editar a mano.

| Campo | Qué es |
|---|---|
| `id` | `dueño/repo:archivo:línea:nombre` (único) |
| `repo`, `commit` | repo público y commit EXACTO analizado |
| `file`, `line`, `last_line` | dónde está (líneas desde 1) |
| `name`, `type`, `class` | nombre, `function`/`method`/`property`, clase que lo contiene |
| `static_priority`, `confidence`, `visibility`, `location` | lo que dijeron Vulture y nuestra clasificación ANTES del cruce |
| `code` | el código (decoradores + cuerpo, máx. 60 líneas, con número de línea) |
| `label` | **la respuesta**: `falso_positivo` o `muerto_probable` |
| `split` | `practica` (para mejorar el prompt) o `final` (solo para la nota) |

**La IA nunca ve `label`** (ni la cobertura): es la clave de respuestas.

## Qué tan confiables son las respuestas

- `falso_positivo` = los tests del repo **ejecutaron** ese código -> está
  vivo. Etiqueta SEGURA.
- `muerto_probable` = los tests **nunca** lo ejecutaron -> probablemente
  muerto, pero podría ser código vivo sin tests. Etiqueta PROBABLE: medir
  aparte el acierto en cada clase.

## Reglas para que la nota sea honesta

- Se divide **por repo** (ningún repo está en los dos grupos).
- Mejorar el prompt mirando SOLO `practica`; la nota que vale es la de
  `final`, medida al final (si se mira `final` para ajustar, deja de ser
  un examen).

## Regenerar (Git Bash, con el clúster kind arriba)

El script LEE código de repos ajenos (no lo ejecuta), pero igual corre en
un contenedor (regla del proyecto): sin privilegios, disco de solo lectura.

```bash
export MSYS_NO_PATHCONV=1
docker run --rm --user 65534:65534 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges -v "$PWD:/w:ro" -v "$PWD/evals/triage:/out" -w /w \
  -e PYTHONPATH=/w/src --entrypoint python deadcode-service:dev \
  scripts/build_triage_exam.py --api http://host.docker.internal:8080 --out /out/examen.jsonl
```

## Licencias

Los trozos de código vienen de repos públicos de código abierto (cada caso
dice su `repo` y `commit`); se usan solo como datos de evaluación.
