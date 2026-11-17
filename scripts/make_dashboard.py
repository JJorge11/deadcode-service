"""Genera el dashboard de Grafana: charts/deadcode-service/dashboards/deadcode.json

Este script es la FUENTE: el JSON (~800 líneas) se genera con él.
Para cambiar el dashboard:
  1. editar este archivo
  2. uv run python scripts/make_dashboard.py --write
  3. commit de los dos archivos
Un test (tests/test_dashboard.py) falla si el JSON no coincide con lo
que genera este script.

Otros usos:
  uv run python scripts/make_dashboard.py --exprs   (lista las consultas, para probarlas)
"""

import json
import sys
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "charts" / "deadcode-service" / "dashboards" / "deadcode.json"

DS = {"type": "prometheus", "uid": "prometheus"}

panels = []
next_id = 1
y = 0


def row(title):
    global next_id, y
    panels.append({"type": "row", "title": title, "id": next_id, "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})
    next_id += 1
    y += 1


def panel(kind, title, desc, targets, x, w, h=8, unit=None, thresholds=None, extra=None):
    global next_id
    defaults = {}
    if unit:
        defaults["unit"] = unit
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": [
            {"color": c, "value": v} for v, c in thresholds]}
        defaults["color"] = {"mode": "thresholds"}
    p = {
        "type": kind, "title": title, "description": desc, "id": next_id, "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [{"datasource": DS, "refId": chr(65 + i), "expr": e, "legendFormat": l,
                     **({"instant": True, "range": False} if kind in ("stat", "bargauge") else {})}
                    for i, (e, l) in enumerate(targets)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {},
    }
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "textMode": "value_and_name"}
    if kind == "bargauge":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "orientation": "horizontal", "displayMode": "gradient"}
    if kind == "timeseries":
        p["options"] = {"legend": {"displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "multi"}}
    if extra:
        p.update(extra)
    panels.append(p)
    next_id += 1


GREEN, YELLOW, RED = "green", "yellow", "red"

# --- API ------------------------------------------------------------------
row("API (peticiones HTTP)")
panel("timeseries", "Peticiones por segundo, por ruta",
      "Sin /health: Kubernetes lo consulta cada 5 s y taparía el resto.",
      [('sum by (route) (rate(deadcode_http_requests_total{route!="/health"}[5m]))', "{{route}}")],
      x=0, w=10, unit="reqps")
panel("stat", "% de errores 5xx (5 min)",
      "Respuestas 500-599 sobre el total. 4xx no cuenta: es culpa del cliente (URL inválida...).",
      # "or vector(0)": sin ningún 5xx la suma queda vacía ("No data"); así muestra 0.
      [('100 * (sum(rate(deadcode_http_requests_total{status=~"5.."}[5m])) or vector(0))'
        ' / clamp_min(sum(rate(deadcode_http_requests_total[5m])), 1e-9)', "5xx")],
      x=10, w=4, unit="percent", thresholds=[(None, GREEN), (1, YELLOW), (5, RED)])
panel("timeseries", "Tiempo de respuesta p95, por ruta",
      "El 95% de las peticiones tardó MENOS que esto. /health incluye revisar Postgres y Redis.",
      [("histogram_quantile(0.95, sum by (le, route) "
        "(rate(deadcode_http_request_duration_seconds_bucket[5m])))", "{{route}}")],
      x=14, w=10, unit="s")
y += 8

# --- Análisis ---------------------------------------------------------------
row("Análisis (leídos de Postgres y Redis: max() porque las 2 réplicas de la API reportan lo mismo)")
panel("stat", "En cola", "Análisis esperando un worker en Redis.",
      [("max(deadcode_queue_jobs)", "en cola")],
      x=0, w=4, h=6, thresholds=[(None, GREEN), (5, YELLOW), (20, RED)])
panel("stat", "Pendiente más viejo",
      "Cuánto lleva esperando el análisis pendiente más antiguo. Si sube sin parar: worker atascado o caído.",
      [("max(deadcode_analyses_oldest_pending_seconds)", "espera")],
      x=4, w=4, h=6, unit="s", thresholds=[(None, GREEN), (60, YELLOW), (300, RED)])
panel("bargauge", "Análisis por estado (total)", "Todos los análisis guardados en Postgres.",
      [("max by (status) (deadcode_analyses)", "{{status}}")],
      x=8, w=8, h=6)
panel("bargauge", "Fallos por motivo (total)",
      "Motivos fijos (ver analysis_metrics.py). 'inesperado' = bug o problema de infraestructura: revisar logs del worker.",
      [("max by (reason) (deadcode_analyses_failed_total) > 0", "{{reason}}")],
      x=16, w=8, h=6, thresholds=[(None, RED)])
y += 6
panel("timeseries", "Análisis terminados y fallidos (por hora)",
      "Cuántos terminan bien y cuántos fallan en cada hora.",
      # deadcode_analyses es gauge: delta (no increase, que es para counters).
      [('max(delta(deadcode_analyses{status="finished"}[1h]))', "terminados"),
       ("sum(max by (reason) (increase(deadcode_analyses_failed_total[1h])))", "fallidos")],
      # 0 decimales: Prometheus extrapola a la hora completa (1,02 en vez de 1).
      x=0, w=12, extra={"fieldConfig": {"defaults": {"decimals": 0, "min": 0}, "overrides": []}})
panel("timeseries", "Duración de los análisis por nivel (p50 y p95, última hora)",
      "Tiempo total: espera en cola + análisis. Solo los terminados bien. Separado por nivel: "
      "uno de nivel 2 (con tests) tarda minutos. Sin análisis en la hora = sin datos.",
      # "by (le, level)": una curva por nivel (sin level, los mezclaría).
      [("histogram_quantile(0.50, max by (le, level) (rate(deadcode_analysis_duration_seconds_bucket[1h])))",
        "nivel {{level}} p50"),
       ("histogram_quantile(0.95, max by (le, level) (rate(deadcode_analysis_duration_seconds_bucket[1h])))",
        "nivel {{level}} p95")],
      x=12, w=12, unit="s")
y += 8

# --- Nivel 2 ----------------------------------------------------------------------
row("Nivel 2: tests del repo con coverage.py (si falla, el análisis igual entrega lo de Vulture)")
panel("stat", "Nivel 2 que funcionó (total)",
      "De los análisis con tests, en cuántos se pudieron correr los tests y cruzar con Vulture.",
      [('100 * max(deadcode_level2_analyses_total{status="finished"})'
        " / clamp_min(sum(max by (status) (deadcode_level2_analyses_total)), 1e-9)", "funcionó")],
      x=0, w=4, h=6, unit="percent", thresholds=[(None, RED), (50, YELLOW), (80, GREEN)])
panel("stat", "Falsos positivos de Vulture",
      "De los hallazgos que coverage pudo juzgar (funciones y métodos), qué % SÍ se ejecutó en los tests: "
      "Vulture decía 'no se usa' y se equivocaba.",
      [('100 * max(deadcode_level2_findings_total{verdict="falso_positivo"})'
        ' / clamp_min(max(deadcode_level2_findings_total{verdict="falso_positivo"})'
        ' + max(deadcode_level2_findings_total{verdict="confirmado"}), 1e-9)', "falsos positivos")],
      x=4, w=4, h=6, unit="percent")
panel("bargauge", "Nivel 2: fallos por motivo (total)",
      "Por qué no se pudieron correr los tests (ver LEVEL2_FAILURE_REASONS en analysis_metrics.py). "
      "'tests_no_ejecutables' suele ser un repo que pide una base de datos o claves.",
      [("max by (reason) (deadcode_level2_failed_total) > 0", "{{reason}}")],
      x=8, w=8, h=6, thresholds=[(None, RED)])
panel("bargauge", "Cruce Vulture + coverage (hallazgos, total)",
      "confirmado = Vulture 'no se usa' y los tests nunca lo ejecutaron; falso_positivo = sí se ejecutó; "
      "no_aplica = coverage no puede opinar (imports, variables, clases...).",
      [("max by (verdict) (deadcode_level2_findings_total)", "{{verdict}}")],
      x=16, w=8, h=6)
y += 6

# --- Sandboxes y salud -------------------------------------------------------
row("Sandboxes y salud de la app")
panel("timeseries", "Sandboxes vivos",
      "Pods en deadcode-sandboxes. Debe volver a 0 tras cada análisis; si se queda arriba: sandbox huérfano.",
      [('count(kube_pod_info{namespace="deadcode-sandboxes"}) or vector(0)', "sandboxes")],
      x=0, w=8, extra={"fieldConfig": {"defaults": {"decimals": 0, "min": 0}, "overrides": []}})
panel("stat", "¿Se pudo leer cada fuente?", "1 = OK; 0 = Postgres o Redis no respondieron a /metrics.",
      [("min by (source) (deadcode_metrics_source_up)", "{{source}}")],
      x=8, w=4, thresholds=[(None, RED), (1, GREEN)])
panel("stat", "Reinicios (última hora)", "Reinicios de contenedores en el namespace deadcode.",
      [('sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="deadcode"}[1h])) > 0'
        ' or vector(0)', "{{pod}}")],
      x=12, w=4, thresholds=[(None, GREEN), (1, YELLOW), (3, RED)], extra={"fieldConfig": {"defaults": {
          "decimals": 0, "thresholds": {"mode": "absolute", "steps": [
              {"color": GREEN, "value": None}, {"color": YELLOW, "value": 1}, {"color": RED, "value": 3}]},
          "color": {"mode": "thresholds"}}, "overrides": []}})
panel("timeseries", "Memoria por pod", "Memoria real (working set) de cada pod de la app.",
      [('sum by (pod) (container_memory_working_set_bytes{namespace="deadcode", container!=""})', "{{pod}}")],
      x=16, w=8, unit="bytes")

dashboard = {
    "uid": "deadcode-overview",
    "title": "deadcode-service",
    "description": "API, análisis y sandboxes. Generado desde git (charts/deadcode-service/dashboards).",
    "tags": ["deadcode"],
    "timezone": "browser",
    "editable": False,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "schemaVersion": 39,
    "version": 1,
    "panels": panels,
    "templating": {"list": []},
    "annotations": {"list": []},
}

def render() -> str:
    """El JSON tal como debe quedar en el archivo."""
    return json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    if "--exprs" in sys.argv:
        for p in panels:
            for t in p.get("targets", []):
                print(f"{p['title']}\t{t['expr']}")
    elif "--write" in sys.argv:
        OUTPUT.write_text(render(), encoding="utf-8", newline="\n")
        print(f"Escrito: {OUTPUT}")
    else:
        sys.stdout.write(render())
