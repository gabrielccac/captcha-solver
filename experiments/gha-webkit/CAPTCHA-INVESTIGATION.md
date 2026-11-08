# Investigação da confirmação do captcha — 2026-11-05

## O que a falha anterior comprova

O erro `captcha_selection_not_confirmed` em
[42057247451, tentativa 3](https://github.com/gabrielccac/captcha-solver/actions/runs/42057247451/attempts/3)
ocorreu na segunda rodada, antes da submissão do desafio e antes de POSTs de
captcha/senha por HTTP. A rodada usou a classificação em lote já obtida: houve
um acerto de cache, sem nova chamada de visão. O detector visual esperava quatro
seleções e não confirmou em seis quadros / 2.568 ms.

Não havia imagens nem índices observados salvos nessa execução. Portanto, não
é possível provar retrospectivamente se faltou um clique, se o detector perdeu
uma marca ou se faltou tempo de estabilidade. Esse código não é um erro HTTP do
modelo nem um veredito do gov.br sobre as imagens escolhidas. Uma classificação
semântica incorreta também não pode ser descartada a partir daqueles registros.

## Evidência coletada e correções

**1. Confirmação de estabilidade começava tarde.** Um teste de regressão reproduziu
uma seleção que ficava correta nos dois últimos quadros antes de acabar o
orçamento. O código anterior ainda esperava um quadro adicional e recusava.
O relógio agora começa no primeiro quadro correto. A confirmação continua exigindo
dois quadros consecutivos com o conjunto exato, diferença de pixels abaixo de
0,5, intervalo estável de 120 ms e mínimo de 180 ms. Marca extra, marca faltante
ou grade em movimento continuam bloqueando. Isso corrige um cenário reproduzido;
não demonstra que ele causou especificamente a falha histórica.

**2. Margem do cabeçalho invalidava o cache.** Na primeira coleta, o cabeçalho
passou de 400×123 para 400×130. Os pixels dos primeiros 123 pixels de altura eram
idênticos; a extensão continha margem/borda de foco. O redimensionamento para
comparação produziu diferença média 14,80, acima do limite 3, e disparou uma
segunda chamada de visão de 2,034 s. Reproduzindo o mapeamento offline, as respostas
do lote e da chamada extra foram exatamente `[1,4,5,7]`; essa sessão foi aceita.

`same_header` aceita esse caso somente com prefixo pixel-identical, margem curta,
linhas extras uniformes de UI e base branca. Mudanças no texto, referência ou
conteúdo adicional continuam invalidando o cache. O teste não relaxa a comparação
de referências. A correção é deliberadamente conservadora: variações que não
atendam a esses critérios continuam gerando uma chamada por rodada.

**3. Observabilidade limitada ao desafio.** O diagnóstico grava índices escolhidos,
cliques, marcas observadas e recortes de tarefa/grade. Não grava página completa,
CPF, senha, tokens, URLs ou corpos HTTP. Capturas ficam fora do Git e os artefatos
foram removidos do GitHub após cópia local. O fluxo normal ganha somente os campos
`expected_selection`, `observed_selection` e `failure_phase` (`marks`/`stability`)
para distinguir falhas futuras sem depender de imagens.

## Resultados, incluindo falhas

[Controle instrumentado](https://github.com/gabrielccac/captcha-solver/actions/runs/42064028287):
duas sessões válidas e uma falha `captcha_http_not_accepted`. Na terceira, as duas
rodadas foram submetidas e o token foi capturado, mas o handoff HTTP 200 não
retornou o formulário de senha. É outro estágio de falha, e o corpo da resposta
não foi retido; não há evidência suficiente para atribuir a causa a expiração,
perfil, classificação ou rejeição do token. Nenhuma senha foi enviada nesse caso.
O diagnóstico posterior registra apenas indicadores booleanos de mensagens de
captcha inválido/sessão expirada para diferenciar novas ocorrências.

Na amostra também houve divergência de uma imagem entre uma resposta em lote e
uma classificação posterior de nove imagens. Isso mostra variabilidade de
classificação, mas não identifica a causa da falha histórica nem fornece rótulo
oficial para aquela imagem. A comparação de precisão exige uma base rotulada.

[Build isolado da correção](https://github.com/gabrielccac/captcha-solver/actions/runs/42067474802):
90 testes dentro do Docker passaram, além de renderização sem rede e verificação
das fontes. A comparação completa entre base e candidata mostrou diferenças
somente em `/app/src/captcha/interactions.py` e `/app/src/captcha/prefetch.py`,
preservando permissões, proprietários e configuração do container. Nenhuma
dependência foi instalada/atualizada. Os 98 testes locais selecionados passaram.

| Execução corrigida | Resultado | Login interno | Total do Actions |
|---|---|---:|---:|
| [Instrumentada 1](https://github.com/gabrielccac/captcha-solver/actions/runs/42068817963/attempts/1) | HTTP 200 no INSS | 46,13 s | 75 s |
| [Instrumentada 2](https://github.com/gabrielccac/captcha-solver/actions/runs/42068817963/attempts/2) | HTTP 200 no INSS | 31,47 s | 65 s |
| [Instrumentada 3](https://github.com/gabrielccac/captcha-solver/actions/runs/42068817963/attempts/3) | HTTP 200 no INSS | 32,24 s | 63 s |
| [Fluxo rápido normal](https://github.com/gabrielccac/captcha-solver/actions/runs/42071512125) | HTTP 200 no INSS | 34,75 s | 64 s |

As duas últimas instrumentadas usaram uma chamada de visão e cache na segunda
rodada. A primeira teve duas chamadas (8,24 s e 3,29 s); o fluxo normal também
manteve fallback e duas chamadas. A redução do desperdício é situacional, não uma
prova de ganho fixo no tempo total. As coletas de diagnóstico incluem gravação e
upload de recortes, além de runners/tarefas diferentes. Quatro sucessos não
demonstram uma taxa de sucesso de produção nem eliminam a falha de handoff observada.

## Estado e próximos pontos

A imagem fixada no fluxo rápido é
`ghcr.io/gabrielccac/captcha-solver-webkit-smoke@sha256:62c0e4242870ff30eefae56bd35c446eaba02d093b5dea3f397018496e7103ba`.
WebKit 27.2, fontes, perfil, viewport, CPU/memória, transporte e verificações de
autenticação foram preservados. Não houve implantação na VPS. A integração de
entrega de tokens por callback/MCP continua separada.

O maior corte adicional na partida exige imagem já disponível em executor
preparado. Para a visão, o próximo benchmark deve comparar lote/rodada e resolução
das imagens usando os mesmos exemplos rotulados, medindo latência e precisão
juntas. A variabilidade do fornecedor continua visível nas chamadas de 1,8–8,3 s
coletadas; trocar o modelo sem esse controle não comprova uma melhoria.
