"""
Cache de análises por hash SHA-256.

A chave inclui: bytes dos arquivos + diretrizes + dossie_obra + modo + tipo_obra + modelo.
Inclui itens_pendentes na chave para evitar servir resultado defasado se a lista mudar.

Persistência: dicionário em memória (simples, para o Render grátis).
O Make é responsável por persistência duradoura via campo cache_analises no Sheets.
Degradação graciosa: qualquer falha retorna None (cache miss), nunca quebra a análise.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Cache em memória — sobrevive enquanto o processo viver no Render
_cache: dict[str, dict] = {}


def calcular_chave(
    arquivos_bytes: list[bytes],
    contexto: dict,
) -> str:
    h = hashlib.sha256()
    for b in sorted(arquivos_bytes, key=len):  # ordem determinística por tamanho
        h.update(b)
    campos_chave = {
        "diretrizes": sorted(contexto.get("diretrizes") or []),
        "dossie_obra": contexto.get("dossie_obra") or "",
        "itens_pendentes": contexto.get("itens_pendentes") or [],
        "modo": contexto.get("modo", "diagnostico"),
        "tipo_obra": contexto.get("tipo_obra", "indefinido"),
        "modelo": contexto.get("modelo", ""),
    }
    h.update(json.dumps(campos_chave, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()


def buscar(chave: str) -> dict | None:
    try:
        entrada = _cache.get(chave)
        if entrada:
            logger.info("Cache hit: %s", chave[:16])
        return entrada.get("resultado") if entrada else None
    except Exception as e:
        logger.warning("Falha ao ler cache: %s", e)
        return None


def salvar(chave: str, resultado: dict) -> None:
    try:
        _cache[chave] = {
            "chave_hash": chave,
            "resultado": resultado,
            "criado_em": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("Falha ao salvar cache: %s", e)


def metadados(chave: str) -> dict:
    """Retorna chave_hash e criado_em para auditoria (sem o resultado)."""
    try:
        entrada = _cache.get(chave, {})
        return {
            "chave_hash": entrada.get("chave_hash", chave),
            "criado_em": entrada.get("criado_em", ""),
        }
    except Exception:
        return {"chave_hash": chave, "criado_em": ""}
