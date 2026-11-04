# Preparação do WebKit no GitHub Actions

O workflow `webkit-pull-benchmark.yml` compara quatro empacotamentos da mesma
imagem validada. Nenhum pacote, navegador, fonte ou código de autenticação é
atualizado durante a preparação.

| Variante | Alteração |
|---|---|
| baseline | Digest original, gzip, camadas originais |
| clean | Sistema de arquivos final copiado para uma camada gzip, removendo metadados APT e dados mortos das camadas antigas |
| zstd | Camadas originais recomprimidas com Zstandard nível 6 |
| repack | `docker-repack` 0.5.0, Zstandard nível 14, alvo de 50 MB por camada, mais restauração dos caminhos Unicode de certificados CA |

`prepare_benchmark.py` verifica a configuração do container e uma assinatura
dos arquivos, permissões, proprietários e links. A comparação ignora montagens
criadas pelo Docker, `/tmp` e os dois diretórios de metadados APT removidos.
O binário externo é verificado por SHA-256 e recebe somente uma cópia OCI local,
sem credenciais de registro em seu ambiente.

A versão 0.5.0 do repack alterou nomes/links Unicode de um certificado NetLock.
A assinatura detectou cinco diferenças de caminho/link e bloqueou a primeira
execução. `Dockerfile.repack-repair` restaura os diretórios de certificados a
partir do digest original e remove os dois nomes incorretos. A variante corrigida
precisa passar novamente pela comparação completa; o repack bruto é rejeitado.

Cada variante executa cinco vezes em runners `ubuntu-24.04` novos. As rodadas
intercalam as quatro variantes e permitem até quatro jobs simultâneos. O script
recusa uma imagem já presente e registra camadas já existentes, modelo de CPU e
versão do runner. Prontidão inclui `docker pull`, os 59 testes offline de auth e
a abertura/renderização/interação do WebKit sem rede. A verificação completa
dos arquivos ocorre depois da medição de prontidão.

Os artefatos contêm somente metadados de imagem e métricas offline. Este workflow
não recebe CPF, senha, chave de visão nem tokens de login. A validação real usa
o workflow de smoke separado e seu ambiente temporário restrito.

Para reproduzir, altere um dos arquivos listados em `paths` no workflow e publique
na branch experimental indicada nele. Depois baixe os artefatos desse run:

```powershell
gh run download RUN_ID --repo gabrielccac/captcha-solver --dir artifacts
python experiments/gha-webkit/summarize_benchmark.py artifacts --output summary.json
```

São cinco amostras por variante: use mediana e intervalo observado. Elas não
comprovam percentis de produção, alta concorrência de logins ou disponibilidade
do gov.br/INSS. O custo de construir/publicar as imagens é separado do custo
recorrente de preparação. As otimizações de empacotamento não reduzem diretamente
o tempo da resolução do captcha.

Referências: [exportadores e compressão do BuildKit](https://docs.docker.com/build/exporters/image-registry/),
[técnicas do docker-repack](https://github.com/orf/docker-repack/blob/v0.5.0/docs/about.md).
