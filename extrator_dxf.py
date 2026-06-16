"""
Extrator de fatos de arquivos DXF para o agente MUD Engenharia.

Modos de operação:
- Normal   (< DXF_STREAMING_MB): ezdxf.recover.readbytes — leitura completa com áreas de polígonos.
- Streaming (DXF_STREAMING_MB .. DXF_MAX_MB): iterdxf — varredura sem carregar o doc na RAM.
- Guarda   (> DXF_MAX_MB): rejeita com aviso controlado, sem estourar memória.
"""

import os
import re
import tempfile
from pathlib import Path

import io

import ezdxf
import ezdxf.recover
from ezdxf.addons import iterdxf

# ── Limites (configuráveis via env) ──────────────────────────────────────────
DXF_STREAMING_THRESHOLD = int(os.getenv("DXF_STREAMING_MB", "15")) * 1024 * 1024
DXF_MAX_SIZE = int(os.getenv("DXF_MAX_MB", "60")) * 1024 * 1024

# ── Tabela INSUNITS → (nome, fator para converter unidade² em m²) ────────────
_INSUNITS: dict[int, tuple[str, float]] = {
    0: ("indefinido", 1.0),
    1: ("polegadas", 0.0254 ** 2),
    2: ("pés", 0.3048 ** 2),
    3: ("milhas", 1609.34 ** 2),
    4: ("mm", 1e-6),
    5: ("cm", 1e-4),
    6: ("m", 1.0),
    7: ("km", 1e6),
    10: ("jardas", 0.9144 ** 2),
    14: ("decímetros", 0.01),
    15: ("decâmetros", 100.0),
    16: ("hectômetros", 10000.0),
}

_GENERIC_LAYER = re.compile(r"^(0|defpoints|default|layer\d*|camada\d*)$", re.IGNORECASE)
_AREA_TEXT = re.compile(r"(\d[\d\s]*[.,]\d+|\d+)\s*m[²2]?", re.IGNORECASE)
_STD_BLOCK = re.compile(r"^[A-Z]{2,}_")


def _area_factor(insunits: int) -> float:
    _, f = _INSUNITS.get(insunits, ("indefinido", 1.0))
    return f


def _shoelace(pts: list) -> float:
    """Fórmula do laçador para área de polígono."""
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts[i][0] * pts[j][1]
        area -= pts[j][0] * pts[i][1]
    return abs(area) / 2.0


def _parse_area_texts(texts: list[dict]) -> list[float]:
    vals: list[float] = []
    for t in texts:
        for m in _AREA_TEXT.finditer(t.get("texto", "")):
            try:
                vals.append(float(m.group(1).replace(" ", "").replace(",", ".")))
            except ValueError:
                pass
    return vals


# ── Modo normal ───────────────────────────────────────────────────────────────

def _extract_from_doc(doc) -> dict:
    header = doc.header
    insunits: int = header.get("$INSUNITS", 0)
    unit_name, factor = _INSUNITS.get(insunits, ("indefinido", 1.0))

    all_layers = list(doc.layers)
    total_layers = len(all_layers)
    generic_count = sum(1 for la in all_layers if _GENERIC_LAYER.match(la.dxf.name))

    has_paperspace = any(
        list(layout)
        for layout in doc.layouts
        if layout.name != "Model"
    )

    msp = doc.modelspace()
    entity_counts: dict[str, int] = {}
    layers: set[str] = set()
    block_counts: dict[str, int] = {}
    texts: list[dict] = []
    polygons: list[float] = []

    for entity in msp:
        etype = entity.dxftype()
        entity_counts[etype] = entity_counts.get(etype, 0) + 1
        layer = entity.dxf.get("layer", "0")
        layers.add(layer)

        if etype == "INSERT":
            bname = entity.dxf.get("name", "")
            block_counts[bname] = block_counts.get(bname, 0) + 1

        if etype in ("MTEXT", "TEXT"):
            try:
                txt = entity.plain_mtext() if etype == "MTEXT" else entity.dxf.get("text", "")
                if txt and txt.strip():
                    texts.append({"camada": layer, "texto": txt.strip()[:300]})
            except Exception:
                pass

        if etype == "LWPOLYLINE":
            try:
                closed = entity.closed or bool(entity.dxf.get("flags", 0) & 1)
                if closed:
                    pts = list(entity.get_points("xy"))
                    if len(pts) >= 3:
                        area_m2 = float(_shoelace(pts) * factor)
                        if area_m2 >= 0.1:  # descarta símbolos minúsculos
                            polygons.append(round(area_m2, 2))
            except Exception:
                pass

    # Alertas de qualidade
    alertas: list[str] = []
    if generic_count > 5:
        alertas.append(
            f"{generic_count} camadas com nome genérico: classificação por disciplina não confiável."
        )
    if not has_paperspace:
        alertas.append(
            "Sem entidades em paperspace: provavelmente sem carimbo/legenda no arquivo."
        )
    all_blocks = list(doc.blocks)
    total_blocks = len(all_blocks)
    non_std = sum(
        1 for b in all_blocks
        if not _STD_BLOCK.match(b.name) and not b.name.startswith("*")
    )
    if total_blocks > 10 and non_std / total_blocks > 0.7:
        alertas.append(
            "Maioria dos blocos sem prefixo de disciplina: nomenclatura não padronizada."
        )

    area_vals = _parse_area_texts(texts)
    polygons_sorted = sorted(polygons, reverse=True)

    return {
        "arquivo": {
            "dxf_version": doc.dxfversion,
            "insunits": insunits,
            "unidade": unit_name,
            "total_camadas": total_layers,
            "total_entidades": sum(entity_counts.values()),
        },
        "camadas": sorted(layers),
        "entidades": entity_counts,
        "blocos": block_counts,
        "textos": texts[:100],
        "areas_poligonos_fechados": {
            "qtd": len(polygons),
            "soma_m2": round(sum(polygons), 2) if polygons else None,
            "maiores_m2": polygons_sorted[:10],
        },
        "areas_anotadas_texto": {
            "qtd": len(area_vals),
            "valores_m2": [round(v, 2) for v in sorted(area_vals, reverse=True)[:20]],
        },
        "alertas_qualidade": alertas,
    }


# ── Modo streaming ────────────────────────────────────────────────────────────

def _extract_streaming(caminho: Path) -> dict:
    entity_counts: dict[str, int] = {}
    layers: set[str] = set()
    block_counts: dict[str, int] = {}
    texts: list[dict] = []

    try:
        tagger = iterdxf.opendxf(str(caminho))
        for entity in tagger.modelspace():
            etype = entity.dxftype()
            entity_counts[etype] = entity_counts.get(etype, 0) + 1
            layer = "0"
            if hasattr(entity, "dxf"):
                layer = entity.dxf.get("layer", "0")
            layers.add(layer)
            if etype == "INSERT" and hasattr(entity, "dxf"):
                bname = entity.dxf.get("name", "")
                block_counts[bname] = block_counts.get(bname, 0) + 1
            if etype in ("MTEXT", "TEXT") and hasattr(entity, "dxf"):
                try:
                    txt = str(entity.dxf.get("text", "") or "")
                    if txt.strip():
                        texts.append({"camada": layer, "texto": txt.strip()[:300]})
                except Exception:
                    pass
        tagger.close()
    except Exception:
        pass

    area_vals = _parse_area_texts(texts)
    generic_count = sum(1 for la in layers if _GENERIC_LAYER.match(la))
    alertas = [
        "Arquivo processado em modo streaming (DXF grande): "
        "contagem de polígonos fechados não disponível."
    ]
    if generic_count > 5:
        alertas.append(
            f"{generic_count} camadas com nome genérico: classificação por disciplina não confiável."
        )

    return {
        "arquivo": {
            "dxf_version": "desconhecida (streaming)",
            "insunits": 0,
            "unidade": "indefinido",
            "total_camadas": len(layers),
            "total_entidades": sum(entity_counts.values()),
        },
        "camadas": sorted(layers),
        "entidades": entity_counts,
        "blocos": block_counts,
        "textos": texts[:100],
        "areas_poligonos_fechados": {"qtd": 0, "soma_m2": None, "maiores_m2": []},
        "areas_anotadas_texto": {
            "qtd": len(area_vals),
            "valores_m2": [round(v, 2) for v in sorted(area_vals, reverse=True)[:20]],
        },
        "alertas_qualidade": alertas,
    }


# ── Ponto de entrada público ──────────────────────────────────────────────────

def extrair_fatos(conteudo: bytes, nome_arquivo: str = "arquivo.dxf") -> tuple[dict, list[str]]:
    """
    Extrai fatos do conteúdo binário de um DXF.
    Retorna (fatos_dict, avisos_list).
    """
    avisos: list[str] = []
    tamanho = len(conteudo)

    def _vazio(alertas_extra: list[str]) -> dict:
        return {
            "arquivo": {
                "dxf_version": None, "insunits": 0, "unidade": "indefinido",
                "total_camadas": 0, "total_entidades": 0,
            },
            "camadas": [], "entidades": {}, "blocos": {}, "textos": [],
            "areas_poligonos_fechados": {"qtd": 0, "soma_m2": None, "maiores_m2": []},
            "areas_anotadas_texto": {"qtd": 0, "valores_m2": []},
            "alertas_qualidade": alertas_extra,
        }

    # Guarda de tamanho
    if tamanho > DXF_MAX_SIZE:
        msg = (
            f"Arquivo DXF muito grande ({tamanho // 1024 // 1024} MB > "
            f"{DXF_MAX_SIZE // 1024 // 1024} MB): extração não realizada para proteger a memória."
        )
        avisos.append(msg)
        return _vazio(avisos), avisos

    # Modo streaming
    if tamanho > DXF_STREAMING_THRESHOLD:
        msg = (
            f"Arquivo DXF processado em modo streaming "
            f"({tamanho // 1024 // 1024} MB > {DXF_STREAMING_THRESHOLD // 1024 // 1024} MB)."
        )
        avisos.append(msg)
        tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
        try:
            tmp.write(conteudo)
            tmp.close()
            fatos = _extract_streaming(Path(tmp.name))
        finally:
            os.unlink(tmp.name)
        fatos["alertas_qualidade"] = avisos + [
            a for a in fatos.get("alertas_qualidade", []) if a not in avisos
        ]
        return fatos, avisos

    # Modo normal
    try:
        doc, audit = ezdxf.recover.read(io.BytesIO(conteudo))
        if audit.has_errors:
            msg = (
                f"DXF com {len(audit.errors)} erro(s) de integridade: "
                "leitura com recuperação automática."
            )
            avisos.append(msg)
    except Exception as e:
        msg = f"Erro ao ler DXF '{nome_arquivo}': {e}"
        avisos.append(msg)
        return _vazio([msg]), avisos

    fatos = _extract_from_doc(doc)
    fatos["alertas_qualidade"] = avisos + fatos.get("alertas_qualidade", [])
    return fatos, avisos
