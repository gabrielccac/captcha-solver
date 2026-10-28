# Meu INSS: Android nativo + CDP direto

Experimento separado do fluxo de produção. Usa o Chrome real de um emulador
Android 16/API 36 (Pixel 6, Chrome 133.0.6943.137), sem SeleniumBase, Playwright,
OpenATX ou solver. Dependências Python: `websocket-client==1.9.2`, `mycdp==1.4.0`.
O transporte e a checagem sintética vieram da versão validada da Receita,
commit `7bf7d4528dfdaf6bd2fc7733814b4c163e873e6a` de `gabrielccac/certidoes`.

Com o emulador iniciado, execute a partir de uma pasta descartável e com este
repositório no `PYTHONPATH`:

```sh
python -m experiments.android_cdp.android
```

Forneça `INSS_TEST_CPF` e `INSS_TEST_PASSWORD` pelo ambiente. Não os passe em
argumentos, commits ou logs. O teste verifica CDP em uma página HTTP local
antes de acessar GOV.BR e limpa o perfil Chrome antes de cada etapa.
Faz no máximo um envio de senha. Para diante de captcha visual persistente,
credenciais inválidas ou verificação em duas etapas; não tenta resolvê-los.

O sucesso exige redirecionamento ao domínio exato do Meu INSS, os dois tokens
de sessão e HTTP 200 na consulta autenticada já usada pelo projeto. Somente
o status HTTP é retornado; o corpo da resposta não é lido nem exportado.
Não executa extração de benefícios, pagamentos ou documentos médicos.
Relatórios em `runs/android-cdp-login/` contêm apenas estados e contadores,
sem screenshots, CPF, senha ou tokens. Descarte os dados do AVD ao terminar.

No Docker, reutilize o ambiente e `docker_boot.py` de
`certidoes/experiments/receita_direct_cdp_probe`, com o SDK somente leitura,
KVM e um AVD novo. Monte este repositório em `/app` e invoque o bootstrap com
os argumentos `-m experiments.android_cdp.android`. Não publique a porta CDP.
O runtime compartilhado também contém dependências do PDF da Receita;
este experimento utiliza apenas as duas dependências de CDP acima.

Isso testa a aceitação do login nesse ambiente; não presume que o resultado
da Receita se repita no GOV.BR, em todos os IPs ou contas.

## Resultado local em Docker

[Relatório sem dados pessoais](results/docker-local.json), fonte testada
`6fe3164d8273272069c967c8e0e39ac8e81c86b5`: login autenticado com HTTP 200,
um envio de CPF, um envio de senha, nenhum captcha visual detectado e nenhum
solver. O teste de login levou 99,79 segundos; boot do Android 67,20 segundos
e espera adicional pelos provedores Google 33,57 segundos. A checagem
sintética de CDP passou antes do portal. Container, AVD e arquivo temporário
de credenciais foram removidos. Não foram extraídos dados da conta.

Antes desse login, duas pré-checagens encerraram sem acessar o GOV.BR.
A segunda registrou `ApplicationExitInfo reason=12 (DEPENDENCY DIED)`: o
Android matou o Chrome porque `com.google.android.gms.persistent` excedeu o
prazo de publicação dos content providers. A memória disponível no Android
era aproximadamente 1,8 GB. A preparação passou a aguardar os provedores
AccountContentProvider, Phenotype e Chimera publicados, com três amostras
estáveis do PID do GMS e limite de 180 segundos, antes de iniciar o Chrome.
Essa execução passou após a correção. O transporte CDP não precisou mudar.

A dependência entre morte de providers e encerramento de seus clientes
é definida no [ContentProviderHelper do Android](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/core/java/com/android/server/am/ContentProviderHelper.java).
Os logs completos de diagnóstico ficaram apenas na pasta local ignorada.

Isso valida uma execução real deste login nesse ambiente. Não comprova
consistência entre contas, IPs ou novas execuções e não remove o solver do
fluxo de produção. A checagem de prontidão recebeu depois um teste adicional
para não confundir o índice de authorities com um provider publicado.
