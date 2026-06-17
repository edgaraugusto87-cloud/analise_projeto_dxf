"""
Agente HTTP MUD Engenharia — FastAPI
Endpoints: GET /health | POST /extrair-dxf | POST /analisar
"""

import base64
import json
import os
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from analise import DEFAULT_MODEL, chamar_claude, montar_conteudo
from cache import buscar, calcular_chave, metadados, salvar
from extrator_dxf import extrair_fatos
from extrator_xlsx import extrair_planilha

# ── Segurança ─────────────────────────────────────────────────────────────────

_API_KEY = os.environ.get("API_KEY", "")


def verificar_chave(x_api_key: Annotated[str | None, Header()] = None):
    if not _API_KEY:
        raise RuntimeError("Variável de ambiente API_KEY não configurada no servidor.")
    if x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail={"erro": "nao autorizado"})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _termina_com(nome: str, *sufixos: str) -> bool:
    """Detecção de tipo robusta a extensões duplas (ex: arquivo.dxf.dxf)."""
    n = nome.lower()
    return any(n.endswith(s) for s in sufixos)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Agente MUD Engenharia",
    description="Analisa projetos de obra e prepara a visita técnica.",
    version="1.2.0",
)


# ── /health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── /extrair-dxf ─────────────────────────────────────────────────────────────

@app.post("/extrair-dxf", dependencies=[Depends(verificar_chave)])
async def extrair_dxf(arquivo: UploadFile = File(...)):
    """Extrai fatos crus de um DXF sem chamar o Claude. Útil para testes."""
    if not _termina_com(arquivo.filename, ".dxf"):
        raise HTTPException(status_code=400, detail={"erro": "Envie um arquivo .dxf"})

    conteudo = await arquivo.read()
    fatos, avisos = extrair_fatos(conteudo, arquivo.filename)
    return {"fatos": fatos, "avisos": avisos}


# ── /analisar ─────────────────────────────────────────────────────────────────

@app.post("/analisar", dependencies=[Depends(verificar_chave)])
async def analisar(
    contexto: str = Form(...),
    arquivo_dxf: Optional[UploadFile] = File(default=None),
    arquivo_pdf: Optional[UploadFile] = File(default=None),
    planilha: Optional[UploadFile] = File(default=None),
):
    """
    Análise completa. Recebe até três arquivos nomeados (todos opcionais):
    - arquivo_dxf : projeto em DXF
    - arquivo_pdf : projeto em PDF
    - planilha    : escopo em .xlsx
    Mais o campo de texto `contexto` (JSON).
    O modo (diagnostico/validacao) é decidido pela presença da planilha.
    """
    try:
        ctx: dict = json.loads(contexto)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail={"erro": f"contexto JSON inválido: {e}"})

    modelo = ctx.get("modelo", DEFAULT_MODEL)
    avisos_gerais: list[str] = []
    fatos_por_arquivo: list[dict] = []
    blocos_multimodal: list[dict] = []
    arquivos_bytes: list[bytes] = []
    planilha_escopo: str = ""

    # ── DXF ──────────────────────────────────────────────────────────────────
    if arquivo_dxf is not None:
        conteudo = await arquivo_dxf.read()
        if conteudo:
            arquivos_bytes.append(conteudo)
            if _termina_com(arquivo_dxf.filename, ".dxf"):
                fatos, avisos = extrair_fatos(conteudo, arquivo_dxf.filename)
                fatos_por_arquivo.append({"arquivo": arquivo_dxf.filename, **fatos})
                avisos_gerais.extend(avisos)
            else:
                avisos_gerais.append(
                    f"arquivo_dxf '{arquivo_dxf.filename}' ignorado: extensão não reconhecida."
                )

    # ── PDF ──────────────────────────────────────────────────────────────────
    if arquivo_pdf is not None:
        conteudo = await arquivo_pdf.read()
        if conteudo:
            arquivos_bytes.append(conteudo)
            if _termina_com(arquivo_pdf.filename, ".pdf"):
                dados_b64 = base64.standard_b64encode(conteudo).decode()
                blocos_multimodal.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": dados_b64},
                })
            elif _termina_com(arquivo_pdf.filename, ".png", ".jpg", ".jpeg"):
                media_type = "image/png" if _termina_com(arquivo_pdf.filename, ".png") else "image/jpeg"
                dados_b64 = base64.standard_b64encode(conteudo).decode()
                blocos_multimodal.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": dados_b64},
                })
            else:
                avisos_gerais.append(
                    f"arquivo_pdf '{arquivo_pdf.filename}' ignorado: extensão não reconhecida."
                )

    # ── Planilha XLSX ─────────────────────────────────────────────────────────
    if planilha is not None:
        conteudo = await planilha.read()
        if conteudo:
            arquivos_bytes.append(conteudo)
            if _termina_com(planilha.filename, ".xlsx", ".xls"):
                escopo, avisos_xlsx = extrair_planilha(conteudo, planilha.filename)
                planilha_escopo = escopo
                avisos_gerais.extend(avisos_xlsx)
            else:
                avisos_gerais.append(
                    f"planilha '{planilha.filename}' ignorada: extensão não reconhecida (esperado .xlsx)."
                )

    if not fatos_por_arquivo and not blocos_multimodal and not planilha_escopo:
        raise HTTPException(
            status_code=400,
            detail={"erro": "Nenhum arquivo válido recebido (DXF, PDF/PNG, XLSX)."},
        )

    # ── Modo decidido pelo agente, não pelo Make ──────────────────────────────
    modo = "validacao" if planilha_escopo else "diagnostico"
    ctx["modo"] = modo
    ctx["planilha_escopo"] = planilha_escopo

    # ── Cache ─────────────────────────────────────────────────────────────────
    chave = calcular_chave(arquivos_bytes, ctx)
    resultado_cache = buscar(chave)
    if resultado_cache is not None:
        meta = metadados(chave)
        resultado_cache.setdefault("avisos", [])
        resultado_cache["avisos"] = (
            [f"Resposta servida do cache (gerada em {meta.get('criado_em', '?')})."]
            + resultado_cache["avisos"]
        )
        return JSONResponse(content=resultado_cache)

    # ── Chamada ao Claude ─────────────────────────────────────────────────────
    conteudo_msg = montar_conteudo(ctx, fatos_por_arquivo, blocos_multimodal)

    try:
        resultado = chamar_claude(conteudo_msg, modelo=modelo)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"erro": f"Falha na chamada ao Claude: {e}"})

    avisos_claude = resultado.get("avisos", [])
    resultado["avisos"] = avisos_gerais + avisos_claude

    salvar(chave, resultado)

    return JSONResponse(content=resultado)
