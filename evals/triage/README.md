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

## Revisión humana (`revision.json`)

Los 92 casos `muerto_probable` se revisaron a mano (2026-11-20), con
búsqueda del nombre en TODO el repo del commit exacto (en contenedor).
Criterio:

- `vivo`: lo llama Python solo (`__dir__`, `__eq__`, `writelines`...),
  un framework (callbacks de keras/dask, `load_tests` de unittest,
  `_repr_pretty_` de IPython), se alcanza por nombre armado
  (`globals()[f"_{name}"]`) o es API pública OBSOLETA que sigue
  funcionando (avisa con `DeprecationWarning`).
- `muerto`: interno, 0 usos en todo el repo y ningún protocolo lo llama.
- `dudoso`: API pública sin uso interno (depende de los usuarios de la
  librería; no se puede saber desde el repo) -> NO entra en la nota.

`scripts/eval_triage.py` usa la etiqueta revisada cuando existe.

## Conclusiones (2026-11-20)

| | Casos |
|---|---|
| Hallazgos con veredicto del nivel 2 | 168 (13 repos) |
| Los tests SÍ los ejecutaron (vivos seguros) | 76 |
| Los tests nunca los ejecutaron | 92 |
| ... de esos, vivos según la revisión | 68 |
| ... dudosos (API pública) | 20 |
| ... **muertos de verdad** | **4** |

1. En librerías maduras, **~97% de lo que Vulture marca NO es código
   muerto** (144 vivos vs. 4 muertos entre los casos que se pudieron
   juzgar). Causas: protocolos de Python, frameworks, nombres armados en
   texto (`getattr`) y API pública.
2. La prioridad "alta" de Vulture acertó solo un **4-8%** de las veces.
   Sus reglas tampoco se trasladan entre repos (en el grupo final, los 23
   "alta" eran falsos positivos de sqlparse, que llama sus métodos con
   `getattr(self, "_process_" + nombre)`).
3. El nivel 2 **no confirma** muertes: "los tests nunca lo ejecutaron"
   casi siempre es código vivo sin tests. Por eso la app ya no sube la
   prioridad ni dice "confirmado" (dice "sin ejecutar en los tests").
4. Lo que SÍ es fiable: si los tests ejecutaron algo, está vivo (un
   falso positivo de Vulture demostrado).
5. Conclusión práctica: la herramienta sirve como **lista de candidatos
   para revisar a mano**, no como veredicto. Medirlo así (examen +
   revisión humana) fue lo que lo reveló.
6. La capa de IA (triaje con Ollama/Groq) quedó en pausa: con 144 vivos
   y 4 muertos, responder "vivo" a todo ya saca 97% y el examen no
   distingue un buen motor de uno trivial. Para retomarla hacen falta
   casos muertos REALES (idea: commits "remove unused ..." en la
   historia de repos públicos). El examen y el corrector quedan listos.

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
