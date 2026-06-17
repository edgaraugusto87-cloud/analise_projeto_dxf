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
  "dossie_obra_atualizado": "<dossiê reescrito e consolidado — mesmo porte, não maior>",
  "avisos": []
}
"""

_INSTRUCAO_DOSSIE = """\

DOSSIÊ DA OBRA (memória de continuidade entre pranchas):
Use o dossiê como ponto de partida do seu raciocínio. Não recomece do zero, \
não repita o que já está estabelecido, não contradiga premissas anteriores sem \
sinalizar a mudança explicitamente.

REGRAS DO DOSSIÊ:
- O dossiê NÃO gera itens. Itens vêm apenas do projeto que você está analisando agora.
- O dossiê impede repetir dúvidas já levantadas; não cria dúvidas novas.
- Ao final, reescreva o dossiê de forma consolidada no campo dossie_obra_atualizado. \
Mesmo porte — não acumule texto, consolide: incorpore o que este projeto esclareceu, \
atualize o que mudou, remova o que ficou obsoleto.

Estrutura do dossiê reescrito (seções curtas):
1. Identificação e natureza: o que é a obra, tipo, cliente, escopo geral entendido.
2. Premissas assumidas: o que está sendo assumido na ausência de informação (e a base).
3. O que já está claro: fatos consolidados das pranchas já analisadas.
4. O que ainda falta / em aberto: lacunas de entendimento (NÃO é lista de itens).
5. Pranchas/disciplinas já analisadas: quais documentos já entraram.
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
    diretrizes: list[str] = contexto.get("diretrizes") or []
    itens_pendentes: list[dict] = contexto.get("itens_pendentes") or []
    planilha_escopo: str = contexto.get("planilha_escopo") or ""
    dossie_obra: str = contexto.get("dossie_obra") or ""

    partes: list[str] = [_INSTRUCAO_BASE]

    if dossie_obra:
        partes.append(f"{_INSTRUCAO_DOSSIE}\nDOSSIÊ ATUAL DA OBRA:\n{dossie_obra}")
    else:
        partes.append(
            "\nDOSSIÊ DA OBRA: Esta é a primeira prancha analisada. "
            "Construa o dossiê inicial no campo dossie_obra_atualizado."
        )

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

    if modo == "diagnostico":
        partes.append(
            "\nPOSTURA DO MODO DIAGNÓSTICO:"
            "\nO escopo ainda é desconhecido — nenhuma planilha foi recebida. "
            "Foque em DESCOBRIR: levante hipóteses de escopo com base no projeto, "
            "gere perguntas ao cliente sobre o que será feito, e declare explicitamente "
            "o que está sendo assumido. "
            "NÃO preencha area_intervencao_m2 — deixe null (escopo indefinido)."
        )
    else:
        partes.append(
            "\nPOSTURA DO MODO VALIDAÇÃO:"
            "\nA planilha de escopo é a FONTE DA VERDADE. O escopo já está definido. "
            "Mude de postura: em vez de perguntar o que será feito, CONFIRME em campo "
            "o que já está definido, aponte divergências entre planilha e projeto, "
            "e liste o que falta para fechar o orçamento. "
            "A área de intervenção, quando houver, vem da planilha — nunca redigite número."
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
