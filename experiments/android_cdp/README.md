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
