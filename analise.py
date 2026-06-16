"""
Montagem de prompt e chamada à API do Claude para o agente MUD Engenharia.
"""

import json
import time

import anthropic

DEFAULT_MODEL = "claude-opus-4-8"

# ── Instrução-base estável (não editar para mudar regras de negócio) ──────────
# As regras variáveis chegam via contexto.diretrizes, enviadas pelo Make.

_INSTRUCAO_BASE = """\
Você é um especialista em análise de projetos de obras da MUD Engenharia.
Sua função é preparar a visita técnica e mapear tudo que impede o orçamento \
de ser fechado. Você NÃO orça e NÃO decide: prepara a informação para a equipe decidir.

REGRAS INVIOLÁVEIS:
1. Nunca inventar medidas, áreas ou especificações. Só afirmar o que está legível.
2. Cada item trata de UMA única verificação. Se há duas, dividir em dois itens.
3. Sempre que um elemento for mantido, recomposto, remanejado ou adequado, \
gerar um item para avaliar o estado atual dele (não é óbvio — é decisivo pro orçamento).
4. Descartar APENAS o que for puramente genérico de qualquer visita ("ver o local", \
"fazer medições gerais"). Tudo ligado a elemento, decisão ou documento específico, manter.
5. Separar o que o projeto MOSTRA, o que é INFERÊNCIA e o que só se confirma EM CAMPO.
6. descricao: uma frase, verbo de ação (confirmar, medir, solicitar, validar), ~20 palavras.
7. Modo VALIDAÇÃO: a planilha de escopo é a FONTE DA VERDADE; o projeto localiza e \
acha pendências. Modo DIAGNÓSTICO: o projeto é ponto de partida; declarar explicitamente \
o que está sendo assumido.
8. Responder EXCLUSIVAMENTE com JSON válido, sem texto antes/depois, sem markdown.

O QUE NÃO FAZER:
- NÃO detectar conflitos de execução entre disciplinas (tubo x viga, etc.).
- NÃO preencher status diferente de "pendente" para itens novos.
- NÃO apagar itens pendentes em silêncio — resolvidos viram status "resolvido" \
com resposta explicando por quê.

ENUMS VÁLIDOS:
categoria  : verificar_visita | pergunta_cliente | pendencia_documental | divergencia | risco_custo
disciplina : arquitetura | eletrica | hidro | avac | pci | marcenaria | acabamento | estrutura | logistica | geral
base       : planilha | projeto | cruzamento | inferencia
status     : pendente | verificado | resolvido
"""

_SCHEMA_SAIDA = """\
Responda com este JSON e NADA MAIS (sem cercas de código, sem texto externo):
{
  "obra": {
    "id_orcamento": "<copiar literalmente do contexto>",
    "identificacao": "<nome/descrição da obra identificada nos arquivos>",
    "modo_analise": "<diagnostico|validacao>",
    "tipo_obra": "<tipo>",
    "area_intervencao_m2": <número ou null>,
    "completude_documental": "<o que foi recebido e o que falta>"
  },
  "itens": [
    {
      "item_id": "<novo: it_NNNN gerado pelo agente | existente: preservar o original>",
      "id_orcamento": "<copiar literalmente do contexto>",
      "categoria": "<enum>",
      "disciplina": "<enum>",
      "ambiente": "<nome do ambiente ou geral>",
      "descricao": "<verbo de ação + objeto + contexto, ~20 palavras>",
      "base": "<enum>",
      "impacta_orcamento": true,
      "status": "pendente",
      "resposta": ""
    }
  ],
  "avisos": []
}
"""


# ── Montagem do prompt ────────────────────────────────────────────────────────

def montar_conteudo(
    contexto: dict,
    fatos_por_arquivo: list[dict],
    blocos_multimodal: list[dict],
) -> list[dict]:
    """
    Retorna a lista de blocos de conteúdo para a mensagem do Claude.
    Ordem: arquivos visuais (PNG/PDF) → texto com instrução + fatos.
    """
    id_orcamento = contexto.get("id_orcamento", "")
    modo = contexto.get("modo", "diagnostico")
    tipo_obra = contexto.get("tipo_obra", "indefinido")
    diretrizes: list[str] = contexto.get("diretrizes", [])
    itens_pendentes: list[dict] = contexto.get("itens_pendentes", [])
    planilha_escopo: str = contexto.get("planilha_escopo", "")

    partes: list[str] = [_INSTRUCAO_BASE]

    if diretrizes:
        partes.append(
            "\nDIRETRIZES ESPECÍFICAS DESTA OBRA:\n"
            + "\n".join(f"- {d}" for d in diretrizes)
        )

    partes.append(
        f"\nCONTEXTO DA OBRA:"
        f"\n- ID: {id_orcamento}"
        f"\n- Tipo: {tipo_obra}"
        f"\n- Modo de análise: {modo.upper()}"
    )

    if fatos_por_arquivo:
        partes.append("\nFATOS EXTRAÍDOS DOS ARQUIVOS DXF:")
        for fatos in fatos_por_arquivo:
            partes.append(json.dumps(fatos, ensure_ascii=False, indent=2))

    if planilha_escopo:
        partes.append(
            f"\nPLANILHA DE ESCOPO (FONTE DA VERDADE NO MODO VALIDAÇÃO):\n{planilha_escopo}"
        )

    if itens_pendentes:
        partes.append(
            "\nITENS PENDENTES DE RODADAS ANTERIORES (reconciliar obrigatoriamente):\n"
            + json.dumps(itens_pendentes, ensure_ascii=False, indent=2)
        )
        partes.append(
            "\nREGRAS DE RECONCILIAÇÃO:"
            "\n- Item pendente ainda válido → manter, PRESERVANDO o item_id original."
            "\n- Item pendente resolvido pelo novo documento → status 'resolvido' + "
            "resposta explicando."
            "\n- Nunca apagar item pendente em silêncio."
            "\n- Item novo → gerar item_id novo (it_NNNN) com status 'pendente'."
        )

    partes.append(_SCHEMA_SAIDA)

    texto_prompt = "\n".join(partes)

    # Blocos visuais primeiro, texto por último
    content = list(blocos_multimodal)
    content.append({"type": "text", "text": texto_prompt})
    return content


# ── Chamada à API do Claude ───────────────────────────────────────────────────

def _extrair_json(raw: str) -> dict:
    texto = raw.strip()
    if "```json" in texto:
        texto = texto.split("```json")[1].split("```")[0].strip()
    elif "```" in texto:
        texto = texto.split("```")[1].split("```")[0].strip()
    return json.loads(texto)


def chamar_claude(
    conteudo: list[dict],
    modelo: str = DEFAULT_MODEL,
    max_tentativas: int = 3,
) -> dict:
    """Chama a API do Claude e devolve o dict parseado."""
    client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY do ambiente

    for tentativa in range(max_tentativas):
        try:
            response = client.messages.create(
                model=modelo,
                max_tokens=8192,
                messages=[{"role": "user", "content": conteudo}],
            )
            raw = response.content[0].text
            return _extrair_json(raw)

        except anthropic.RateLimitError:
            if tentativa < max_tentativas - 1:
                espera = 5 * (2 ** tentativa)
                time.sleep(espera)
            else:
                raise

        except json.JSONDecodeError as e:
            if tentativa < max_tentativas - 1:
                time.sleep(2)
            else:
                raise ValueError(f"Claude retornou JSON inválido após {max_tentativas} tentativas: {e}")

    raise RuntimeError("Falha após todas as tentativas de chamada ao Claude.")
