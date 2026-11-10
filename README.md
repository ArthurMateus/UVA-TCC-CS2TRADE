# CS2 Trade-Up Lab

Motor de análise de **contratos de troca (trade-up) do Counter-Strike 2**,
gratuito e executado localmente. Em vez de ordenar por valor esperado, que
favorece "bilhetes de loteria" com 3% de chance de pagar, ele procura
contratos com **alta chance de lucro numa única execução** e, no modo
Garantido, contratos em que **todo resultado possível supera o custo**.

Projeto do Trabalho de Conclusão de Curso em Ciência da Computação da
**Universidade Veiga de Almeida (Rio de Janeiro, 2026)**, de Arthur Ferreira da
Silva Mateus e Igor Machado. A monografia está em [`docs/tcc/`](docs/tcc/).

---

## Uso rápido (Windows)

| Passo | O que fazer |
|---|---|
| 1. Instalar (uma vez) | Dois cliques em **`instalar.bat`**: cria o ambiente Python, instala tudo, baixa o catálogo de skins e cria o `.env` |
| 2. Configurar o banco (opcional) | No `.env`, troque `[SUA-SENHA]` em `DATABASE_URL` pela senha do banco Supabase |
| 3. Coletar preços (deixe rodando) | Dois cliques em **`coletar.bat`**. Varre a Steam em rodadas, salva tudo localmente e envia ao Supabase só o que mudou. Ctrl+C para parar; ao reabrir, continua de onde parou |
| 4. Ver os resultados | Dois cliques em **`abrir_site.bat`** (abre http://127.0.0.1:8000) |

Pelo terminal, os mesmos passos são `tradeup setup`, `tradeup collect` e
`tradeup serve`.

- **Moeda:** BRL por padrão (troque com `TRADEUP_CURRENCY=USD` no `.env`).
- **Duração da coleta:** uma rodada completa de todas as raridades leva cerca
  de 2 horas (6 s entre páginas, para não ser bloqueado pela Steam), e a
  próxima começa 60 min depois. Ajuste com, por exemplo,
  `tradeup collect --interval 120 --rarities milspec,restricted`.
- **Log:** `data/collect.log`.
- **Energia:** desative a suspensão automática do Windows (Configurações →
  Sistema → Energia) enquanto estiver coletando.

### Usar o site em outro computador

O Supabase é a fonte compartilhada. Só **um** computador deve rodar o
`coletar.bat`. Nos demais:

1. Clone o repositório e rode o `instalar.bat`.
2. No `.env`, preencha `DATABASE_URL` com a senha e defina
   `TRADEUP_PRICE_SOURCE=supabase`.
3. Use apenas o `abrir_site.bat`.

O site relê o banco a cada 5 minutos e mostra os preços da última rodada
concluída pelo computador que coleta.

---

## Ideias principais

### 1. O valor esperado encontra bilhetes de loteria

Ordenar por valor esperado (EV) destaca contratos como este (exemplo
ilustrativo):

```
custo 101,80   EV 118,55   ROI 116%     <- parece ótimo
  M4A4 | Modern Hunter (WW)   50%   236,19
  M4A4 | Sheet Lightning (FT) 25%     1,00
  Glock-18 | Glockingbird(FT) 25%     0,81
```

Quase todo esse EV vem de uma única skin. Você perde dinheiro em metade das
execuções, e toda a vantagem depende de um único preço ilíquido estar certo.
O EV só é a métrica certa quando dá para repetir o contrato vezes suficientes
para a média aparecer, e a R$ 100 por execução ninguém consegue.

**Por isso o sistema otimiza para uma única execução.** A métrica principal é
`P(lucro)`, a chance de *um* contrato dar lucro, e o modo Garantido só devolve
contratos que não podem dar prejuízo.

### 2. Contratos garantidos têm solução em forma fechada

Um contrato não tem risco quando o *resultado mais barato possível* ainda
supera o que você pagou. Num contrato de uma única coleção, todos os
resultados são igualmente prováveis, então:

```
garantido  ⟺  min(líquido dos resultados) > custo total das entradas
           ⟺  líquido(resultado mais barato) > n × preço da entrada
           ⟺  preço da entrada < líquido(resultado mais barato) / n
```

Isso é uma restrição que se resolve diretamente. Para cada coleção, o sistema
pega o resultado mais barato do nível seguinte, em **qualquer desgaste
alcançável**, e calcula o teto de preço das entradas. **O(coleções), poucos
milissegundos**, em vez de uma busca combinatória. Como usa o pior desgaste
possível, essa garantia **não depende do float** das entradas.

Todos os valores são **líquidos da taxa do mercado** (5% + 10% da Steam por
padrão; `--fee` para outros mercados).

> Com os preços reais coletados em novembro de 2026 e a taxa de 13% da Steam,
> o modo Garantido encontrou **0 contratos**. Com a taxa de 2% do CSFloat,
> encontrou 5 em Confidencial (Classified). Os dados de exemplo em `data/sample_*.json` são
> fictícios e servem só para os testes.

### 3. A coleta de preços se ajusta sozinha

Um ritmo fixo de requisições é rápido demais (acumula bloqueios HTTP 429,
cada um com espera exponencial) ou lento demais (uma varredura leva horas).
Não existe constante correta: o limite varia com o endpoint, o horário e o
quanto o seu IP já consumiu.

O coletor usa **AIMD**, o mesmo controle de congestionamento do TCP:

| Evento | Resposta |
|---|---|
| 25 respostas sem erro | ritmo `+2`/min |
| qualquer 429 | ritmo `× 0,5` e aumentos congelados por 90 s |
| 5 erros 429 seguidos | o disjuntor (*circuit breaker*) abre e todo o tráfego para |
| fim da espera | UMA requisição de teste (*half-open*); se for bloqueada de novo, a espera dobra |
| 4 disparos do disjuntor | a execução para e informa o que faltou |

Além disso:

- **Fila de trabalho:** um pool de workers (6 por padrão) puxa os itens de uma
  fila, então um item em espera não trava os outros.
- **Sem rajadas:** um balde de tokens com rajada máxima de 2 evita disparar
  várias requisições depois de uma pausa.
- **Cache negativo de 24 h:** guarda os itens sem anúncio. Um HTTP 500 isolado
  é tentado de novo antes de ser tratado como "sem anúncio", porque a Steam
  também responde 500 em falhas temporárias.
- **Nada some em silêncio:** todo item termina como *ok*, *vazio* ou
  *desistência*.

Detalhe importante: requisições simultâneas devolvem vários 429 para um
*único* evento de congestionamento. Reduzir o ritmo a cada resposta o levaria
ao mínimo depois de um só incidente, então o sistema faz no máximo uma redução
a cada 10 segundos, como o TCP reduz uma vez por ida e volta.

### 4. O risco fica visível, não escondido numa média

Cada contrato informa:

| Métrica | Significado |
|---|---|
| **P(lucro)** | chance de uma única execução dar lucro |
| **pior caso** | quanto você recebe no resultado mais barato |
| **grau** | garantido · forte · equilibrado · arriscado · loteria |
| **perda esperada** | `E[perda \| você perde]` |
| **risco/retorno** | lucro esperado por unidade de perda no pior caso |
| **participação do maior resultado** | acima de 85% significa que a vantagem depende de um único preço |
| **depende do float** | o contrato só funciona comprando entradas com float baixo (ver abaixo) |

O `n*` (execuções até o lucro se confirmar estatisticamente) continua sendo
informado, mas supõe que os *preços estão certos* e que só o sorteio é
aleatório. Ele não enxerga uma skin com preço errado; por isso a concentração
é medida à parte.

---

## Por que Python

O gargalo é o **limite de requisições da Steam**, não o processamento. A busca
de contratos garantidos sobre todas as coleções leva poucos milissegundos em
Python, enquanto a coleta de preços de que ela depende leva horas porque o
servidor limita o ritmo. Reescrever em Go ou Rust deixaria rápido o que já é
rápido e não mudaria nada do que importa.

O motor também acumula muitas regras difíceis de acertar: chances por item,
reescala do float em duas etapas, taxa da Steam, regras de Souvenir e
codificação de texto. Reescrever traria todos esses erros de volta. O
front-end é um único arquivo HTML sem dependências: sem build, sem npm,
carrega na hora.

---

## A matemática

### Float: reescalado duas vezes, com o limite de cada skin

O desgaste do resultado é determinístico. Cada entrada é normalizada pelo
**seu próprio** limite de float, as entradas são combinadas pela média e o
resultado é reescalado para o limite **da skin de saída**:

```
float_normalizado = (float − mín) / (máx − mín)
média             = média dos n floats normalizados
float_resultado   = média × (máx_saída − mín_saída) + mín_saída
```

Tirar a média dos floats brutos é o modelo de antes da atualização Retakes e
erra sempre que os limites diferem. Verificado com 8 casas decimais:

| Caso | Esperado | Calculado |
|---|---|---|
| MAC-10 \| Sakkaku 0,22798392 com limite 0,21–0,79 | 0,031007 | 0,031007 |
| 10× P90 \| Trigon 0,749 com limite 0,08–0,75 | 0,99850746 | 0,99850746 |
| → AWP \| Asiimov, limite 0,18–1,00 | 0,998776119 | 0,998776119 |
| → AUG \| Chameleon, limite 0,00–0,50 | 0,499253731 | 0,499253731 |

Um mesmo contrato produz um float diferente (e às vezes um desgaste diferente)
para cada resultado possível, porque cada skin de saída tem seu próprio
limite. O site mostra o limite ao lado de cada skin.

### Chances: por item

```
P(coleção C) = entradas_de_C / n
P(skin s em C) = (entradas_de_C / n) / resultados_em_C
```

Sete de dez entradas de uma coleção com 3 resultados → 70% para a coleção e
23,33% por skin. Uma M4A1-S | Knight entre dez entradas → **10%** fixos de
tirar a Dragon Lore, porque a Cobblestone tem um único resultado no nível
acima. O modelo antigo devolve 5,26% para tudo e só é mantido para comparação
(`--odds outcome_weighted`).

### Taxa: 13% do que o comprador paga

A Valve cobra 5% + 10% **sobre o que o vendedor recebe**, cada parcela
arredondada para baixo e com mínimo de 1 centavo. Como a taxa é somada por
cima, e não descontada:

```
o comprador paga 271,60  →  o vendedor recebe 236,19     (13,04% a menos)
anunciar por 100,00      →  você recebe 86,97
```

O cálculo é feito em centavos inteiros, exatamente como a Valve: o vendedor
recebe o maior valor `s` tal que `s + máx(1, ⌊0,05·s⌋) + máx(1, ⌊0,10·s⌋)`
não passa do preço pago. Multiplicar por `0,85` subestima o que você recebe
em ~2 pontos e elimina receitas no limite. Use `--fee 0.02` para o CSFloat e
`0.08` para a Skinport.

### Souvenir entra, normal sai

Itens Souvenir são entradas válidas desde 22 de maio de 2026. Os atributos são
descartados e o **resultado é sempre de qualidade normal**. Cópias Souvenir
costumam ser o jeito mais barato de completar um contrato. O campo
`souvenir_capable` vem da participação da skin em pacotes Souvenir, não do
campo booleano da fonte de dados (que está marcado em quase todas as skins).

### Preço × float (limitação conhecida)

A Steam informa o preço por **desgaste**, não por float. A busca supõe que
cada entrada tem float baixo dentro do desgaste (`--float-percentile`, 15%),
mas paga o anúncio mais barato do desgaste, que costuma ter float **alto**.
Isso subestima o custo justamente nos contratos cujo lucro depende de o
resultado cair num desgaste melhor. Com os dados reais, todos os 20 melhores
contratos de cada raridade dependem disso.

Como o sistema lida com isso hoje:

- **Indicador de dependência:** cada contrato informa a **folga de float**,
  isto é, quanto a média pode subir sem que algum resultado mude de desgaste,
  e se ele **depende do float** (com as entradas no topo do desgaste, algum
  resultado pioraria).
- **Avisos no site:** o selo "depende do float", o float máximo a comprar em
  cada item ("compre com float ≤ X") e o aviso de que o preço é por desgaste.
- **Garantia robusta:** o modo Garantido usa o pior desgaste possível, então a
  garantia vale qualquer que seja o float.

Solução definitiva (implementada, aguardando a chave de API):
`refine-floats`, ou a caixa *Checagem de float* no site, consulta no CSFloat
os anúncios mais baratos abaixo do float necessário e recalcula o contrato com
**floats e preços reais**. A base padrão `premium` é o preço da Steam
multiplicado pelo ágio de float medido no CSFloat; `--basis csfloat` usa os
preços do próprio CSFloat. Contratos sem anúncios suficientes abaixo do limite
aparecem como *inviáveis*. Requer `CSFLOAT_API_KEY` no `.env`.

---

## Comandos

| Comando | Para quê |
|---|---|
| `setup` | Primeira execução: catálogo e imagens num único download |
| `collect` | **Coleta contínua**: varre todas as raridades em rodadas, atualiza o snapshot, envia ao Supabase o que mudou, espera e repete |
| `bulk-warm` | Preços de uma raridade inteira via **busca paginada filtrada por raridade**, com checkpoints para retomar |
| `warm` | Preços + volume de vendas em 24 h (priceoverview), concorrente e adaptativo |
| `guaranteed` | Contratos que não podem dar prejuízo (líquidos de taxa) |
| `scan` | Busca ranqueada, filtrada pela chance de lucro |
| `refine-floats` | Recalcula os melhores contratos no **float que as entradas realmente precisam** (CSFloat) |
| `db-push` | Envia catálogo e cotações ao Supabase (PostgreSQL) |
| `sync-db` / `sync-images` | Catálogo de skins e imagens separadamente |
| `serve` | Interface web |

### Paginação: menos requisições, sem lacunas

O `search/render` da Steam devolve no máximo **10 linhas por página**, seja
qual for o `count` pedido, então varrer o catálogo inteiro custaria ~3,5 mil
requisições. O coletor filtra no próprio servidor com as tags da Steam
(`category_730_Rarity[]=tag_Rarity_Rare_Weapon` para Mil-Spec,
`category_730_Quality[]=tag_normal|tag_tournament`).

- **Tamanho de cada raridade** (normal + Souvenir): Mil-Spec ~2,6 mil linhas;
  todas as raridades somam ~9,2 mil linhas, ou ~923 requisições por rodada.
- **Sem lacunas:** o `start` avança pelo número de linhas realmente recebidas.
- **Retomável:** cada página é gravada e registrada em checkpoint
  (`data/sweep_checkpoints.json`).
- **Sem fim silencioso:** uma página com falha ou repetida marca a varredura
  como *incompleta*, e rodar de novo retoma de onde parou.

A API web também é paginada: `limit` (máx. 100) e `offset`, com `total` e
`next_offset` em toda resposta. Cada busca é calculada uma vez por conjunto de
parâmetros (cache LRU com limite e *single-flight*), e o orçamento é só um
filtro: aumentar o orçamento nunca esconde contratos, e mudar de página ou de
filtro não recalcula nada.

### Coleta contínua e resiliência

- **Envio incremental:** o `collect` envia ao Supabase só as cotações que
  mudaram (preço, ou mais de 10% no número de anúncios), mais um registro por
  item por dia.
- **Espera progressiva:** rodadas bloqueadas dobram a espera, até 4 h.
- **Envio refeito:** falhas de envio são refeitas na rodada seguinte.
- **Uma coleta por computador:** uma trava impede duas coletas ao mesmo tempo
  na mesma máquina.
- **Arquivos à prova de interrupção:** os arquivos são gravados de forma
  atômica, então disco cheio ou queda de energia não os corrompe.
- **Disco quase cheio:** a rodada é pulada se houver menos de 200 MB livres.

### Interface web

Toda em português, com tema claro/escuro automático, layout para celular e
navegação por teclado. Dois modos:

- **Ranqueado** (padrão): a busca geral, com filtros de chance mínima de lucro,
  ROI e orçamento.
- **Garantido**: só contratos em que todo resultado supera o custo.

Cada card abre uma **barra de dispersão dos resultados**: cada resultado é um
ponto e o seu custo é uma linha laranja; pontos à esquerda da linha dão
prejuízo. Limites de float, anúncios disponíveis, imagens, lucro por
resultado e o aviso de dependência de float aparecem no próprio card. Um
glossário explica as métricas para quem não conhece o jogo.

---

## Estrutura

```
UVA-TCC-CS2TRADE/
├── README.md  LIQUIDITY.md  pyproject.toml  requirements.txt  pytest.ini
├── instalar.bat  coletar.bat  abrir_site.bat     atalhos para Windows
├── .env.example                 modelo de configuração (copie para .env)
├── docs/tcc/                    monografia (docx limpo, docx com alterações, pdf)
├── supabase/schema.sql          tabelas, RLS e gatilhos (pode rodar mais de uma vez)
├── data/
│   ├── sample_skins.json        VERSIONADO  dados fictícios para os testes
│   ├── sample_prices.json       VERSIONADO  dados fictícios para os testes
│   ├── skins.json               gerado pelo setup / sync-db
│   ├── images.json              gerado pelo setup / sync-images
│   ├── prices.sqlite            cache local de preços
│   └── price_snapshot.json      cache exportado, usado pelas buscas e pelo site
├── tradeup/
│   ├── models.py                Skin, InputItem, Outcome, faixas de desgaste
│   ├── engine.py                float, chances, taxas, EV            [PURO]
│   ├── risk.py                  perfil de risco + busca de garantidos
│   ├── pricing.py               cotação → preço utilizável
│   ├── optimizer.py             busca heurística orientada a risco
│   ├── float_pricing.py         dependência de float e segunda passada
│   ├── collector.py             coleta contínua em rodadas
│   ├── config.py                caminhos e leitura do .env
│   ├── fsutil.py                gravação atômica de arquivos
│   ├── liquidity.py             níveis de liquidez (ainda fora das buscas)
│   ├── images.py                índice de imagens
│   ├── cli.py                   todos os comandos
│   ├── web.py                   API FastAPI paginada
│   ├── static/index.html        interface em um único arquivo
│   ├── data/loader.py           catálogo de skins (CSGO-API)
│   └── prices/
│       ├── steam.py             cache, cotações e leitura de preços
│       ├── bulk.py              varredura paginada por raridade, checkpoints
│       ├── fetcher.py           coletor assíncrono AIMD com disjuntor
│       ├── csfloat.py           anúncios com float (API do CSFloat)
│       └── pg_store.py          espelho no Supabase/PostgreSQL
└── tests/                       212 testes, todos offline
```

O `engine.py` só importa `models.py` e a biblioteca padrão. Os preços chegam
por uma função de callback, por isso a suíte inteira roda sem rede.

---

## Instalação manual

```powershell
git clone https://github.com/ArthurMateus/UVA-TCC-CS2TRADE.git
cd UVA-TCC-CS2TRADE
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,supabase]"
pytest                          # 212 testes, offline, com os dados de exemplo
copy .env.example .env          # depois preencha as chaves que você tiver
tradeup setup
```

Requer Python 3.10+. Um clone novo roda e passa nos testes **sem acesso à
rede**. Os preços de exemplo são fictícios: qualquer ROI que apareça antes da
primeira coleta não significa nada.

**Windows:** nomes de skins têm caracteres que a codificação padrão do Windows
(cp1252) não representa, então toda leitura e gravação usa
`encoding="utf-8"` e o console é reconfigurado ao iniciar.

---

## Limites de requisição: leia antes de coletar

O bloqueio `429` da Steam é **por IP**, dura horas e **recomeça a contar toda
vez que você insiste durante o bloqueio**. Tentar de novo só piora.

O `collect`, o `bulk-warm` e o `warm` lidam com isso sozinhos: começam com
cuidado, aceleram enquanto não há erro, reduzem o ritmo ao primeiro bloqueio e
param se o bloqueio se repetir. Se o disjuntor disparar, pare e volte mais
tarde; reiniciar o roteador para trocar de IP também resolve.

Ctrl+C é sempre seguro: cada cotação é gravada assim que chega, e a próxima
execução busca só o que ainda falta.

---

## Na prática

O saldo da carteira Steam não pode ser sacado. Um trade-up lucrativo feito só
na Steam vira mais crédito na Steam, não dinheiro. Lucro real exige um mercado
com saque, o que muda a taxa (2–8% em vez de 13%) e as hipóteses de liquidez;
as duas coisas são configuráveis.

O trade-up consome as entradas para sempre. Um contrato **garantido** só é
garantido se os preços forem reais: confira o preço e a quantidade de anúncios
do pior resultado antes de executar, principalmente em Bem Desgastada
(Well-Worn), e verifique o float das entradas que comprar.

## Licença

MIT (declarada no `pyproject.toml`). Sem vínculo com a Valve. Dados de skins da
[CSGO-API](https://github.com/ByMykel/CSGO-API), não oficial.
