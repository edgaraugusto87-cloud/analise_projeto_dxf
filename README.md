# Agente MUD Engenharia

Serviço HTTP que recebe arquivos de projeto (DXF, PNG, PDF) e devolve um JSON estruturado com os pontos que a equipe da MUD precisa verificar antes de orçar. É chamado pelo Make e integrado ao AppSheet/Google Sheets.

---

## Variáveis de ambiente obrigatórias

| Variável | Descrição |
|---|---|
| `API_KEY` | Chave secreta que o Make envia no header `X-API-Key` |
| `ANTHROPIC_API_KEY` | Chave da API da Anthropic |

Variáveis opcionais (têm default):

| Variável | Default | Descrição |
|---|---|---|
| `DXF_STREAMING_MB` | `15` | Acima deste tamanho (MB), usa modo streaming de RAM baixa |
| `DXF_MAX_MB` | `60` | Acima deste tamanho (MB), rejeita o DXF com aviso controlado |

---

## Rodar localmente

```bash
# 1. Criar ambiente virtual
python -m venv .venv
source .venv/bin/activate

# 2. Instalar dependências
pip install -r requirements.txt

# 3. Configurar variáveis (crie um .env ou exporte)
export API_KEY=minha_chave_secreta
export ANTHROPIC_API_KEY=sk-ant-...

# 4. Subir o servidor
uvicorn main:app --reload
```

Acesse `http://localhost:8000/health` — deve retornar `{"status": "ok"}`.

---

## Endpoints

### `GET /health`
Verificação de vida. Sem autenticação.

```
curl http://localhost:8000/health
```

---

### `POST /extrair-dxf`
Extrai fatos crus de um DXF sem chamar o Claude. Útil para testes.

```bash
curl -X POST http://localhost:8000/extrair-dxf \
  -H "X-API-Key: minha_chave_secreta" \
  -F "arquivo=@planta.dxf"
```

Resposta:
```json
{
  "fatos": {
    "arquivo": { "dxf_version": "AC1018", "insunits": 4, "unidade": "mm", ... },
    "camadas": ["0", "ARQUITETURA", ...],
    "entidades": { "LINE": 3135, "MTEXT": 269 },
    "textos": [...],
    "areas_poligonos_fechados": { "qtd": 12, "soma_m2": 210.5, "maiores_m2": [...] },
    "areas_anotadas_texto": { "qtd": 63, "valores_m2": [6491.54, 95.96] },
    "alertas_qualidade": [...]
  },
  "avisos": []
}
```

---

### `POST /analisar`
Análise completa. Este é o endpoint que o Make chama em produção.

**Entrada:** `multipart/form-data`
- `arquivos` — um ou mais arquivos (DXF, PNG, PDF)
- `contexto` — campo texto com JSON (ver esquema abaixo)

```bash
curl -X POST http://localhost:8000/analisar \
  -H "X-API-Key: minha_chave_secreta" \
  -F "arquivos=@planta.dxf" \
  -F "arquivos=@planta.pdf" \
  -F 'contexto={
    "id_orcamento": "ORC-2026-0042",
    "modo": "validacao",
    "tipo_obra": "hospitalar",
    "diretrizes": [
      "Sempre avaliar conformidade RDC 50 nas áreas críticas.",
      "Padrão da unidade gera pendência documental do caderno de padrões."
    ],
    "planilha_escopo": "texto extraído da planilha...",
    "itens_pendentes": [
      { "item_id": "it_0007", "categoria": "pendencia_documental",
        "descricao": "Solicitar projeto elétrico complementar." }
    ]
  }'
```

**Campos do contexto:**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `id_orcamento` | string | sim | Identificador do orçamento (copiado em cada item) |
| `modo` | `diagnostico` \| `validacao` | sim | Sem planilha = diagnóstico; com planilha = validação |
| `tipo_obra` | string | sim | `hospitalar`, `corporativo`, `residencial`, `indefinido` |
| `modelo` | string | não | ID do modelo Claude (default: `claude-opus-4-8`) |
| `diretrizes` | array de strings | não | Regras específicas desta obra, vindas do AppSheet |
| `itens_pendentes` | array de objetos | não | Itens de rodadas anteriores para reconciliar |
| `planilha_escopo` | string | não | Texto da planilha de escopo (ativa modo validação) |

**Resposta:**
```json
{
  "obra": {
    "id_orcamento": "ORC-2026-0042",
    "identificacao": "Reforma Posto de Coleta - Unidade CPS Betim",
    "modo_analise": "validacao",
    "tipo_obra": "hospitalar",
    "area_intervencao_m2": 210,
    "completude_documental": "planilha + arquitetura; faltam complementares"
  },
  "itens": [
    {
      "item_id": "it_0012",
      "id_orcamento": "ORC-2026-0042",
      "categoria": "verificar_visita",
      "disciplina": "avac",
      "ambiente": "sala de coleta",
      "descricao": "Confirmar se equipamentos de climatização fornecidos pela Unimed já estão no local.",
      "base": "planilha",
      "impacta_orcamento": true,
      "status": "pendente",
      "resposta": ""
    }
  ],
  "avisos": []
}
```

---

## Deploy no Render

1. Crie um **Web Service** apontando para este repositório Git.
2. Configure:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Environment:** Python 3.11+
3. Adicione as variáveis de ambiente `API_KEY` e `ANTHROPIC_API_KEY` no painel do Render.
4. O Render detecta `$PORT` automaticamente — não hardcode a porta.

> **Plano gratuito (512 MB RAM):** o modo streaming de DXF garante operação mesmo com arquivos grandes. Para DXF > 60 MB, o agente devolve um aviso controlado em vez de travar.
> Para escalar RAM (DXF muito complexos ou análises simultâneas), migre para o plano **Standard** ($25/mês, 2 GB RAM).

---

## Fluxo Make → Agente → AppSheet

```
1. Make detecta arquivo novo no Google Drive
2. Make lê diretrizes e itens pendentes do AppSheet/Sheets
3. Make faz POST /analisar com os arquivos + contexto JSON
4. Agente devolve JSON estruturado de itens
5. Make faz upsert na tabela analise_itens do Sheets (chave: item_id)
6. Equipe vê os itens no AppSheet e preenche status/resposta após a visita
```

---

## Estrutura do projeto

```
agente-mud/
├── main.py          # FastAPI: endpoints /health, /extrair-dxf, /analisar
├── extrator_dxf.py  # Extração de fatos do DXF (normal + streaming)
├── analise.py       # Montagem do prompt e chamada ao Claude
├── requirements.txt
└── README.md
```
