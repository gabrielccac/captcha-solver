# Execução rápida no Actions — 2026-11-05

A execução real usa a imagem aprovada por digest, sem checkout nem repetição de
testes de CI por login. Os testes continuam no build/benchmark da imagem. O
worker mantém a verificação das fontes, versão do WebKit, identidade, troca de
código, miToken e endpoint autenticado independente HTTP 200.

## Implementação

- A imagem permanece `sha256:88473eecf5f4d70a012aaca2e3fbd33b3b9147311c8a7a65a343426ca389cb3e`.
- O workflow busca apenas `fast_run.py` por commit imutável, verifica SHA-256 e
  executa o lançador. A transferência medida levou cerca de 0,15–0,3 s.
- A configuração de registro fica em diretório temporário privado, sem a etapa
  separada de `docker login`. As credenciais do login são retiradas do ambiente
  antes de executar os comandos de preparação.
- `docker create` + `docker cp` extraem o lançador original e o seccomp da imagem.
  Ambos precisam corresponder aos hashes aprovados. Não se inicia navegador
  durante essa extração; nos testes ela levou cerca de 0,1 s.
- O runner descartável recarrega `max-concurrent-downloads=10` via SIGHUP,
  preservando a configuração existente. Isso não reinicia o daemon nem altera a
  VPS. [Referência do Docker](https://docs.docker.com/reference/cli/dockerd/#configuration-reload-behavior).
- O worker original permanece com WebKit 27.2, perfil temporário, viewport,
  fontes, seccomp, 1 CPU e 1.400 MB de limite de memória já validados.
- O evento `SESSION_READY` contém somente horários; é emitido depois do HTTP 200
  independente e antes da limpeza do runner. Os tokens continuam sem exportação.

## Benchmark de preparação

[Run 42056387839](https://github.com/gabrielccac/captcha-solver/actions/runs/42056387839):
20/20 jobs passaram, cinco por variante, em runners novos e sem camadas em cache.
Cada job executou os 59 testes e o preflight sem rede. Nas variantes rápidas,
essa validação ocorreu depois de registrar o ponto de prontidão e sem nenhuma
credencial de login. Os quatro testes locais do lançador também passaram,
totalizando 63 testes locais com os testes de auth.

| Variante | Mediana do início do job até pronto para login | Intervalo observado |
|---|---:|---:|
| Controle: checkout + autenticação do registro + CI antes do login | 23,09 s | 20,63–30,57 s |
| Sem checkout/CI, 3 downloads | 17,79 s | 15,19–21,17 s |
| Sem checkout/CI, 6 downloads | 15,26 s | 14,47–19,82 s |
| Sem checkout/CI, 10 downloads | **14,37 s** | 13,55–23,88 s |

A redução mediana contra o controle foi **37,8%**. O controle conserva as
operações do caminho anterior, agrupando os comandos Docker em um passo; não é
uma reprodução de todos os custos entre passos do workflow antigo. A tabela
exclui a fila do GitHub e a validação posterior das variantes rápidas. Os
intervalos se sobrepõem; cinco amostras não demonstram percentis de produção.

## Três tentativas reais, sem retry automático

[Run 42057247451](https://github.com/gabrielccac/captcha-solver/actions/runs/42057247451):

| Tentativa | Até iniciar login, com fila | Até sessão validada, com fila | Até fim do job, com fila | Resultado |
|---|---:|---:|---:|---|
| [1](https://github.com/gabrielccac/captcha-solver/actions/runs/42057247451/attempts/1) | 22,46 s | 55,00 s | 56 s | HTTP 200, access token e miToken presentes |
| [2](https://github.com/gabrielccac/captcha-solver/actions/runs/42057247451/attempts/2) | 27,71 s | 62,44 s | 64 s | HTTP 200, access token e miToken presentes |
| [3](https://github.com/gabrielccac/captcha-solver/actions/runs/42057247451/attempts/3) | 23,35 s | — | 60 s | `captcha_selection_not_confirmed`, antes de POST de captcha/senha por HTTP |

Os dois logins bem-sucedidos levaram internamente 30,93 s e 32,59 s. O worker usou
13,48 e 16,99 CPU-s, com picos de 606,3 e 576,6 MiB. As duas sessões tinham access
token com 3.598 s restantes e foram verificadas no INSS. Na falha, a confirmação
visual foi preservada; não houve envio de senha por HTTP. O resultado real foi
**2 sucessos em 3 tentativas**, sem conclusão sobre uma taxa de sucesso de produção.

A execução anterior observada levou 38 s até iniciar o login e 77 s até terminar;
os totais novos bem-sucedidos foram 56–64 s. Parte da diferença total vem da
variação do login/rede, além da preparação. Todos os ambientes temporários de
segredos foram removidos, sem artefatos de autenticação. O workflow de login
fica desativado entre campanhas; build e benchmarks continuam separados.

## Limites de escopo e atualização

Os tempos reais começam no evento do GitHub. A preparação das credenciais do
teste ocorreu antes do disparo. O código da API/MCP da VPS e seu executor não
foram alterados. A integração Actions → callback → MCP ainda não existe;
`SESSION_READY` mede o ponto em que uma entrega poderia ocorrer, sem implementar
essa entrega. Para obter uma espera próxima somente ao login, o executor precisa
estar preparado previamente; o runner descartável ainda baixa a imagem.

Ao atualizar `fast_run.py`, publique primeiro o novo commit, atualize seu commit
e SHA-256 nos workflows e repita os testes. Ao atualizar a imagem, atualize o
digest aprovado e os hashes do bundle depois da aprovação offline. As fontes e
o navegador devem continuar sujeitos à mesma verificação.
