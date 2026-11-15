"""Errores esperados de un análisis.

Su mensaje se guarda tal cual en la columna "error" y lo ve el usuario,
así que debe ser claro y no revelar detalles internos. Los errores
INESPERADOS (bugs) se guardan solo como "Error inesperado (<tipo>)" y el
detalle completo queda en los logs del worker.
"""


class AnalysisError(Exception):
    """Error esperado: su mensaje se muestra tal cual al usuario."""
