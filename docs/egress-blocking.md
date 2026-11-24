# Bloqueio de egress do runner no POST do CAPTCHA

O POST de uso único que consome o token do hCaptcha pode não atravessar a borda
do GOV.BR dependendo da rede de onde o runner sai. Em duas ondas instrumentadas
(51 execuções válidas, cada uma com uma tentativa), o resultado separou por ASN
de saída e por nada mais:

| Egress do runner | Execuções | Autenticou |
|---|---|---|
| AS8075 (Azure) | 20 | 0 |
| AS14618 / AS16509 (AWS) | 30 | 30 |

Cada execução recebe um IP próprio (27 IPs distintos nas amostras), então a
falha acompanha a faixa, não um endereço. Jobs em faixas diferentes rodaram na
mesma janela de tempo, com o mesmo commit, mesma imagem e mesma conta: os de
AWS passaram enquanto os de Azure falhavam.

O sintoma varia com o transporte, o que durante muito tempo escondeu a causa:

- `replay` (o padrão): `read_timeout` em `http_consume_captcha`, com o POST
  aberto por cerca de 30 s e nenhum byte de resposta.
- `native` (POST enviado pelo browser): `captcha_http_not_accepted`, porque o
  SSO devolve o formulário de conta em HTTP 200 com corpo normal.
- `curl_cffi` impersonando Safari 17: falha igual, o que descarta fingerprint
  de TLS como gatilho.

Nenhuma página desta investigação apresentou o sinal `request_rejected`; a
rejeição chega como re-renderização da tela de login.

## O que não explica a falha

- **A marcação do CAPTCHA.** O hCaptcha emitiu token em 30 de 30 execuções, e
  só emite quando aceita a resposta. A auditoria visual cega de uma amostra de
  8 desafios deu 7 acertos exatos, nenhuma omissão e o único erro numa execução
  que passou.
- **O transporte.** Os três modos falham e passam exatamente nas mesmas faixas.
- **Repetir dentro do job.** As tentativas compartilham o IP: P(recuperar | a
  primeira falhou) ficou em 15,9% contra 77% esperados se fossem independentes,
  e 65% das retentativas repetem o código exato.
- **Regressão de código.** A taxa de falha por commit é plana (28% a 33%) nos
  quatro commits com volume, e estável ao longo do dia (sem rajada).

## Guarda de egress

`deploy/actions/query_run.py` resolve a identidade de saída antes de subir o
container e compara o ASN com `INSS_BLOCKED_ASN` (padrão `AS8075`). Em faixa
bloqueada, o job encerra em cerca de dez segundos com:

```json
{"code": "runner_egress_blocked", "retryable": true, "next_action": "retry_later",
 "blocked_asn": ["AS8075"], "result_class": "technical_failure",
 "egress": {"ip": "...", "org": "AS8075 Microsoft Corporation"}}
```

e exit code 4, que `inss-query.yml` converte em aviso explícito. A consulta de
identidade falha aberta: se o serviço não responder, o login segue, porque a
guarda não pode virar um ponto de falha novo.

Trocar a faixa exige uma decisão de infraestrutura, não de código: fixar a
capacidade do runner fora das faixas bloqueadas, ou sair por um proxy. Enquanto
isso, a guarda transforma uma espera de um a três minutos numa falha de segundos
com motivo explícito, e o reenfileiramento por um novo despacho passa a ser a
estratégia de recuperação. Com três despachos independentes, a disponibilidade
esperada fica em torno de 96% (0,33³).

## Como reproduzir

O laboratório vive na branch `diag/captcha-run-lab`: `.github/workflows/diag-captcha.yml`
roda um login instrumentado por job, lê `diag/plan.json` para montar a matriz e
publica os crops de cada rodada, os picks do modelo, a seleção observada e o
ASN de saída. `diag/collect.py --wave <nome>` baixa os artefatos e resume as
execuções. Nenhum desses artefatos contém token, senha, CPF ou cookies.
