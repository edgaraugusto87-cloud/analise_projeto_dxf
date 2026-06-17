"""
Agente HTTP MUD Engenharia — FastAPI
Endpoints: GET /health | POST /extrair-dxf | POST /analisar
"""

import base64
import json
import os
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from analise import DEFAULT_MODEL, chamar_claude, montar_conteudo
from cache import buscar, calcular_chave, metadados, salvar
from extrator_dxf import extrair_fatos

# ── Segurança ─────────────────────────────────────────────────────────────────

_API_KEY = os.environ.get("API_KEY", "")


def verificar_chave(x_api_key: Annotated[str | None, Header()] = None):
    if not _API_KEY:
        raise RuntimeError("Variável de ambiente API_KEY não configurada no servidor.")
    if x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail={"erro": "nao autorizado"})


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Agente MUD Engenharia",
    description="Analisa projetos de obra e prepara a visita técnica.",
    version="1.1.0",
)


# ── /health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── /extrair-dxf ─────────────────────────────────────────────────────────────

@app.post("/extrair-dxf", dependencies=[Depends(verificar_chave)])
async def extrair_dxf(arquivo: UploadFile = File(...)):
    """
    Extrai os fatos crus de um arquivo DXF sem chamar o Claude.
    Útil para testes e reuso pelo Make.
    """
    if not arquivo.filename.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail={"erro": "Envie um arquivo .dxf"})

    conteudo = await arquivo.read()
    fatos, avisos = extrair_fatos(conteudo, arquivo.filename)
    return {"fatos": fatos, "avisos": avisos}


# ── /analisar ─────────────────────────────────────────────────────────────────

@app.post("/analisar", dependencies=[Depends(verificar_chave)])
async def analisar(
    arquivos: list[UploadFile] = File(...),
    contexto: str = Form(...),
):
    """
    Análise completa: extrai fatos dos DXF, prepara conteúdo multimodal
    de PNG/PDF, chama o Claude e devolve o JSON estruturado de itens.
    """
    # Parseia o JSON de contexto
    try:
        ctx: dict = json.loads(contexto)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail={"erro": f"contexto JSON inválido: {e}"})

    modelo = ctx.get("modelo", DEFAULT_MODEL)
    avisos_gerais: list[str] = []
    fatos_por_arquivo: list[dict] = []
    blocos_multimodal: list[dict] = []
    arquivos_bytes: list[bytes] = []

    # Rastreia extensões para evitar redundância visual (PNG + PDF do mesmo desenho)
    tem_pdf = any(f.filename.lower().endswith(".pdf") for f in arquivos)
    tem_png = any(f.filename.lower().endswith(".png") for f in arquivos)
    pular_png = tem_pdf and tem_png

    for arquivo in arquivos:
        nome = arquivo.filename.lower()
        conteudo = await arquivo.read()
        arquivos_bytes.append(conteudo)

        if nome.endswith(".dxf"):
            fatos, avisos = extrair_fatos(conteudo, arquivo.filename)
            fatos_por_arquivo.append({"arquivo": arquivo.filename, **fatos})
            avisos_gerais.extend(avisos)

        elif nome.endswith(".pdf"):
            dados_b64 = base64.standard_b64encode(conteudo).decode()
            blocos_multimodal.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": dados_b64,
                },
            })

        elif nome.endswith(".png") or nome.endswith(".jpg") or nome.endswith(".jpeg"):
            if pular_png:
                avisos_gerais.append(
                    f"Imagem '{arquivo.filename}' ignorada: PDF do mesmo projeto já incluído "
                    "(evitar redundância visual)."
                )
                continue
            media_type = "image/png" if nome.endswith(".png") else "image/jpeg"
            dados_b64 = base64.standard_b64encode(conteudo).decode()
            blocos_multimodal.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": dados_b64,
                },
            })

        else:
            avisos_gerais.append(f"Arquivo '{arquivo.filename}' ignorado: tipo não suportado.")

    if not fatos_por_arquivo and not blocos_multimodal:
        raise HTTPException(
            status_code=400,
            detail={"erro": "Nenhum arquivo válido recebido (DXF, PNG, PDF)."},
        )

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

    # Injeta avisos de infraestrutura
    avisos_claude = resultado.get("avisos", [])
    resultado["avisos"] = avisos_gerais + avisos_claude

    # Salva no cache (falha silenciosa)
    salvar(chave, resultado)

    return JSONResponse(content=resultado)
