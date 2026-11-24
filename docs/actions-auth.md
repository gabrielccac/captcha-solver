# Autenticação do Meu INSS via GitHub Actions

O recibo identifica a etapa e o tipo das falhas HTTP. Os códigos incluem
`read_timeout`, `connect_timeout`, `tls_error`, `connection_error` e
`invalid_json_response`. Os campos `http_operation`, `http_method`,
`request_error_type` e `last_http_status` permitem localizar a falha sem
expor URL, CPF, cabeçalhos ou conteúdo da resposta.

O recibo inclui tamanho e SHA-256 do JSON antes da criptografia. Saídas acima
do limite de 1 MB geram `worker_output_too_large`. O resultado de autenticação
contém `client_data.api_credentials` em sucesso. O cliente usa essa sessão
para escolher os endpoints, baixar documentos e tratar resultados parciais.
O campo `options` de consulta é recusado com `query_options_not_supported`.

Falhas transitórias na resolução ou entrega do CAPTCHA antes do envio da senha
permitem até três tentativas no mesmo job, sempre com sessão e token novos.
O worker só inicia outra tentativa se houver margem no orçamento de 200 segundos;
o contêiner mantém o limite externo de 240 segundos. Senha enviada, credenciais
recusadas, 2FA e etapas interativas não acionam novas tentativas de autenticação.
`auth_retry` contém quantidade, histórico, resultado e motivo da parada. Os campos
`captcha_retry` e `pre_password_retry` são mantidos para consumidores existentes.
`captcha_terminal` e `password_terminal` identificam o tipo da página recebida,
sem HTML, valores, imagens ou URLs. A assinatura estrutural e os códigos públicos
ERL ajudam a agrupar páginas desconhecidas sem registrar seu conteúdo.

Quando o redirecionamento OAuth termina em HTTP 200 sem o callback esperado,
`oauth_terminal` registra somente sinais limitados da página: formulário de
login/OTP, autorização de dados, atualização cadastral, sessão expirada,
indisponibilidade ou página desconhecida. Não inclui HTML, texto, valores ou URLs.
Se não houver uma etapa interativa identificada, o transporte tenta uma única
retomada SSO com novos `state`/`nonce`, usando somente GETs e os cookies existentes.
`oauth_resume_retry` informa se recuperou. A retomada não abre outro browser,
não resolve outro CAPTCHA e não repete a senha. Senha inválida e 2FA não disparam
esse retry; uma página inconclusiva não comprova credencial inválida.

`credential_status` distingue `valid`, `invalid`, `requires_two_factor` e
`undetermined`. `next_action` orienta `use_credentials`, `check_credentials`,
`complete_two_factor`, `retry_later`, `complete_govbr_step` ou `review_login_flow`.
`retryable` permite programar uma tentativa posterior limitada; não significa
que o retry interno teve sucesso nem autoriza repetição ilimitada.

Algumas faixas de rede do runner não conseguem completar o POST do CAPTCHA: o
job encerra em segundos com `runner_egress_blocked`, `retryable` e o ASN
observado, e a recuperação é um novo despacho em outro runner. Veja
[bloqueio de egress](egress-blocking.md).

O workflow `inss-query.yml` autentica no gov.br, confirma acesso ao Meu INSS
e entrega a credencial da API ao consumidor. As regras de consulta, seleção
de benefícios, cálculo e escrita pertencem ao fluxo do consumidor.

O consumidor pode enviar `sealed` e um `request_id` aleatório de 32 dígitos
hexadecimais. Configure `INSS_SEALED_PRIVATE_KEY` como segredo do ambiente e
distribua somente a chave pública. O envelope `v1.wrapped.nonce.tag.body`
usa campos Base64URL sem padding, chave AES-256-GCM envelopada com RSA-OAEP
SHA-256, nonce de 12 bytes, tag de 16 bytes e AAD `inss-credentials:v1:<request_id>`.
O corpo cifrado contém `request`, `expires` em segundos Unix, `cpf`, `password`
e `result_key` aleatória de 32 bytes em Base64URL. Use validade curta e mantenha
a chave de resultado no consumidor. Para esse modo o artefato traz
`result.sealed` em `v1.nonce.tag.body`, cifrado com AES-256-GCM e AAD
`inss-result:v1:<request_id>`. O JSON decifrado segue o mesmo contrato abaixo.

Dispare o workflow em `main` com um `request_id` opaco. Para autenticar
com dados de uma fila externa, informe também `job_id`, uma referência de uso
único com 32 dígitos hexadecimais. Configure a variável de repositório
`INSS_RELAY_ENVIRONMENT` com o ambiente autorizado a obter essas credenciais.
Nesse ambiente, configure a URL HTTPS `INSS_CLAIM_URL` e o segredo
`INSS_CLAIM_BEARER`. Quando um ambiente já usa outro nome de segredo, a
variável `INSS_CLAIM_SECRET_NAME` aponta para esse nome. A URL recebe
`POST {"job_id": "..."}` autenticado e devolve os campos `GOVBR_CPF` e
`GOVBR_PASSWORD`. Sem `job_id`, o workflow usa a conta de teste previamente
configurada em `n8n-inss-query`. CPF e senha ficam fora dos inputs do Actions.

O artefato `inss-encrypted-result` contém `result.cms`, cifrado para o
certificado do ambiente, e um recibo de status sem material de autenticação.
Após descriptografar `result.cms`, leia:

| Campo | Conteúdo |
|---|---|
| `validation.result_class` | `authenticated`, `client_action_required` ou `technical_failure` |
| `validation.code` | `ok`, `invalid_credentials`, `two_factor_required` ou o código técnico |
| `client_data.api_credentials` | Presente em sucesso, com `base_url`, `cpf`, `headers` e `validity` |

Use os headers retornados apenas com a `base_url` do Meu INSS. Escolha os
endpoints, filtros e datas no consumidor. Consuma o artefato logo após o job e
remova-o após recuperar o resultado. A credencial exportada permite chamadas
enquanto válida. Para renovar, use um serviço que guarde a sessão SSO ou
autentique novamente.
