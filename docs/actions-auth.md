# Autenticação do Meu INSS via GitHub Actions

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
