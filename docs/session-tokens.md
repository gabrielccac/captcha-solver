# Meu INSS: validade, reutilização e renovação de sessão

Revisado em 2026-11-04 a partir do código e das validações locais do projeto.

**O comportamento comprovado é reutilizar uma sessão ainda válida e obter novos
tokens por um novo login quando necessário. Não há um fluxo de `refresh_token`
validado que renove o token principal sem autenticação.**

## O que está publicado e o que ainda é local

Na `main` examinada, commit `bd68eb1`, o login híbrido já está disponível em
[`inss.http_login`](../src/inss/http_login.py). Ele preenche uma
`requests.Session` fornecida pelo chamador e permite reutilizá-la em memória.
O comando `inss-login-http` fecha essa sessão ao terminar; o MCP publicado nessa
revisão ainda executa o fluxo anterior em um subprocesso por chamada.

A integração do MCP com o login híbrido e seu cache em memória foi implementada
e testada **no checkout local**, mas ainda não foi integrada à `main` examinada.
As regras desse cache, descritas abaixo, não devem ser presumidas ao instalar
somente aquela revisão da `main`. Esta publicação é de documentação.

Em 2026-11-04, a [PR #3](https://github.com/gabrielccac/captcha-solver/pull/3)
também está aberta: sua descrição informa consultas unificadas em um worker
isolado, sem cache de tokens. Não confundir essa proposta com o cache local
descrito aqui; publicar ou integrar uma delas exige conferir a implementação
escolhida. Nenhuma dessas descrições comprova um endpoint de refresh.

## Tokens necessários

| Valor | Origem e uso | Validade observada |
| --- | --- | --- |
| Código OAuth | Retorno SSO com `code` e `state`; usado na troca por `access_token` | Não é token de sessão nem mecanismo de renovação; não repetir a troca do mesmo código |
| `access_token` / `ifs_auth` | Token principal enviado como `Authorization: Bearer ...` | 3.600 segundos nos testes |
| `miToken` | Emitido pela API do Meu INSS; enviado junto com `Authorization` | 86.400 segundos nos testes |

Os tempos acima são amostras observadas, não um prazo fixo para qualquer conta.
O código usa o `exp` recebido em cada token. A validade útil é limitada pelo
primeiro que expira: um `miToken` de 24 horas não torna utilizável um
`access_token` que já expirou após uma hora.

## Renovação somente por HTTP, sem reabrir o navegador

Esse é um objetivo diferente de reutilizar tokens válidos ou de enviar a senha
por HTTP no primeiro login. O navegador ser fechado antes do POST de senha
comprova apenas a segunda metade do login inicial; não comprova renovação futura.

Há dois mecanismos a distinguir:

- **Refresh de token:** receber novos tokens usando uma credencial de refresh.
  Não há endpoint/contrato desse fluxo validado ou utilizado no código atual.
  As ocorrências de `refreshToken` no JavaScript do portal capturado em
  2026-10-27 são da biblioteca de instalação do Firebase; não comprovam refresh
  do `access_token` do Meu INSS/GOV.BR.
- **Nova autorização com cookies SSO:** uma sessão HTTP que ainda conserva
  cookies autenticados poderia, em tese, solicitar uma nova autorização e receber
  outro código OAuth sem pedir senha/captcha. Não testamos esse comportamento no
  GOV.BR nem por quanto tempo esses cookies seriam aceitos. O cache local
  descarta a sessão expirada e inicia o login híbrido; não implementa essa via.

Para comprovar a segunda hipótese, um teste precisa preservar a sessão HTTP em
memória, solicitar uma autorização com novo `state`/`nonce`, verificar o callback,
trocar o novo código e conferir identidade, novos `iat`/`exp` e HTTP 200 usando
os novos tokens. Um HTTP 200 com os tokens antigos, ou apenas outro `miToken`,
não demonstra que o token principal foi renovado. Se o SSO voltar a exigir
senha, captcha, consentimento ou OTP, o teste deve registrar essa exigência;
não pode classificá-la como renovação silenciosa bem-sucedida.

`token_claims()` apenas decodifica o JWT e não verifica sua assinatura.
`token_validity()` expõe emissão, expiração e segundos restantes sem retornar os
tokens. O teste de acesso real é uma resposta autenticada HTTP 200 por TLS.
Revogação pode invalidar a sessão antes do `exp`.

## Como a sessão é obtida

O fluxo de [`http_login.py`](../src/inss/http_login.py) é:

1. Chrome/Xvfb fazem o bootstrap, enviam o CPF e executam o hCaptcha.
2. O POST protegido é capturado antes de ser enviado pelo navegador, e consumido
   uma única vez pela sessão HTTP com os cookies e o User-Agent adotados.
3. A resposta deve conter o formulário de senha. Chrome/Xvfb são encerrados.
4. A sessão HTTP envia a senha e segue os redirecionamentos. O código exige que
   o callback tenha o `state` esperado antes de aceitar o código OAuth.
5. O código é trocado pelo token principal:

   ```text
   GET https://vip-pmeuinss-api.inss.gov.br/apis/govbrservices/token/{code}/clientid/autorizar.meu.inss.gov.br
   Query: redirectURL=https://meu.inss.gov.br/
   ```

6. O bootstrap da API obtém `miToken`:

   ```text
   GET https://vip-pmeuinss-api.inss.gov.br/apis/usuarioservices/info/{cpf}?refreshPedidos=false
   Authorization: Bearer <access_token em memória>
   miToken: undefined
   ```

7. A mesma consulta é validada com os dois tokens emitidos. HTTP 200 confirma o
   acesso autenticado, e os cabeçalhos ficam na `requests.Session` do chamador.

`refreshPedidos=false` é o nome de um parâmetro dessa consulta. Não é um
`grant_type=refresh_token`, nem demonstra renovação do token principal.
O bootstrap em si também não comprova que seja possível renovar um `miToken`
expirado usando um token principal ainda válido; esse cenário não foi testado.

## Reutilização e reautenticação no MCP local

Implementação local: `_authenticated_session()` em
[`mcp_server.py`](../src/inss/mcp_server.py). Ela mantém até quatro sessões por
processo MCP, sem salvar cookies, tokens ou cache de sessão em disco.

| Condição | Comportamento implementado localmente |
| --- | --- |
| Mesmo CPF e mesma senha, ambos os tokens com mais de 30 segundos restantes | Faz a consulta autenticada de `info`; reutiliza somente após HTTP 200 |
| `min(exp_access, exp_mi) <= agora + 30 segundos` | Descarta a sessão e faz novo login quando a conta for solicitada novamente |
| Pré-checagem de sessão em cache retorna 401/403 | Descarta e tenta um novo login nessa chamada, sem repetir o envio da senha nesse novo fluxo |
| Extração de dados retorna 401/403 | Invalida o cache e informa o erro; um novo login fica para a próxima chamada |
| Pré-checagem retorna outro status, como 500, ou falha de rede | Informa falha e não inicia outro login automaticamente |
| CPF diferente | Não compartilha a sessão nem os tokens entre contas |
| Senha fornecida diferente | Descarta a sessão anterior e autentica com a nova entrada; não concede acesso usando o cache |
| Credenciais rejeitadas ou segunda etapa exigida | Retorna `invalid_credentials` ou `two_factor_required`; não tenta código OTP |
| Quinta conta em cache | Fecha a sessão mais antiga inserida para manter o limite de quatro |
| Processo MCP encerrado | Fecha as sessões; um novo processo não herda os tokens |

As senhas não ficam no cache: a correspondência usa um HMAC com chave aleatória
do processo. As chamadas são serializadas. Sessões próximas da expiração são
removidas no início de uma chamada; não existe renovação agendada em segundo
plano. Consultar a API com HTTP 200 não estende o prazo armazenado no cache.

`session_reused=true` significa que o login foi evitado. Os pagamentos e PDFs
continuam sendo consultados/baixados novamente. `login_ms` inclui a validação
da sessão reutilizada; `elapsed_ms` inclui a operação completa.

## Evidências e limites

Na validação real de 2026-11-03, duas chamadas no mesmo processo MCP local:

| Chamada | Resultado observado |
| --- | --- |
| Primeira | Novo login híbrido, 49 créditos e quatro cartas; 37,252 s no total |
| Segunda | `session_reused=true`, mesmos 49 créditos e download do laudo escolhido; 7,456 s no total, com 224 ms na validação da sessão |

O reuso foi observado ao vivo. Expiração próxima, 401/403, troca de senha,
isolamento entre CPFs e limite do cache foram verificados em testes simulados
de `tests/test_mcp.py` no checkout local. **Não houve teste real aguardando a
expiração de uma hora, nem comprovação de refresh silencioso.**

Os arquivos das execuções ficam apenas em `runs/`, ignorado pelo Git, e não são
necessários para instalar o projeto. Este documento não contém credenciais,
tokens, protocolos, números de benefícios ou dados clínicos.

Antes de implementar uma renovação diferente do novo login, ainda é necessário
observar uma troca real de refresh, verificar seu contrato e confirmar o acesso
com o token emitido. Não se deve reutilizar código OAuth ou token de captcha
para tentar prolongar uma sessão.
