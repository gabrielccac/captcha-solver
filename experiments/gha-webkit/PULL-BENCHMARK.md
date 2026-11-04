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

## Resultados medidos em 2026-11-04

[Rodada final](https://github.com/gabrielccac/captcha-solver/actions/runs/41931390727):
20/20 jobs de medição passaram. Todos começaram sem a imagem e registraram zero
camadas já existentes. Em cada job passaram os 59 testes offline, o preflight e a
assinatura completa do runtime. WebKit 27.2 e a assinatura das 429 fontes foram
preservados. Os runners tiveram CPUs diferentes, registradas nos artefatos.

| Variante | MB comprimidos (decimal) | Download: mediana [mín–máx], s | Prontidão: mediana [mín–máx], s | Redução da mediana de prontidão |
|---|---:|---:|---:|---:|
| baseline | 551,4 | 20,04 [14,90–20,35] | 23,67 [19,05–24,30] | — |
| clean | 508,1 | 13,93 [11,39–24,32] | 18,69 [18,20–28,72] | 21,0% |
| zstd | 482,5 | 18,00 [13,51–21,04] | 21,55 [17,81–24,65] | 9,0% |
| repack corrigido, OCI/Zstandard | 418,0 | 12,00 [11,24–18,53] | 16,12 [15,29–22,12] | 31,9% |

O ganho mediano do repack foi 7,55 s até a prontidão e 8,04 s no download. O
intervalo observado se sobrepõe ao baseline; cinco amostras não provam um ganho
fixo em qualquer runner. A construção/publicação única e a fila do GitHub não
entram nesta tabela. A assinatura de runtime final foi
`cbc4fbdfa089d7b5d4240be3ae6576e8653ae1002fcb73d8fb425fa657d60923`.

[Login real com zstd](https://github.com/gabrielccac/captcha-solver/actions/runs/41931479837):
sucesso, endpoint INSS independente HTTP 200, access token e miToken presentes,
duas rodadas visuais. Login interno 34,19 s; worker com validação independente
39,92 s; job 68 s; evento até conclusão 72 s. O ambiente temporário de segredos
foi removido e nenhum artefato de autenticação foi publicado.

### Execuções exploratórias e limites

As execuções que falharam não foram misturadas com as 20 medições finais:

- [Preparação original](https://github.com/gabrielccac/captcha-solver/actions/runs/41929471212):
  a conferência bloqueou as alterações Unicode do repack bruto.
- [Diagnóstico dos caminhos](https://github.com/gabrielccac/captcha-solver/actions/runs/41930487652):
  cinco diferenças de caminhos/links; conteúdo do certificado idêntico.
- [Primeira publicação corrigida](https://github.com/gabrielccac/captcha-solver/actions/runs/41930930489):
  um dos cinco jobs do repack apresentou assinatura diferente, embora os testes
  e o render passassem. Essa imagem declarou camadas gzip e foi rejeitada; não
  foi adotada. A causa daquela divergência não foi determinada.
- [Diagnóstico do baseline em quatro runners](https://github.com/gabrielccac/captcha-solver/actions/runs/41931767029):
  snapshots idênticos em quatro modelos de CPU. O workflow temporário foi removido.

A publicação final usa exportador OCI com Zstandard explícito e outro digest.
Passou em 5/5 verificações completas nessa rodada. Nenhum dado desses testes
comprova escalabilidade de autenticação em produção; o teste é de preparação CI.
