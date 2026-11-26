# Custo de uma execução

Medido no container de produção (`--cpus 2 --memory 1400m --shm-size 256m
--tmpfs /tmp:512m`), Chrome headed sob Xvfb, egress Hetzner, novembro de 2026.
Todas as execuções terminaram em `ok`.

| Recurso | Medido |
|---|---|
| Tempo de parede | 26–39 s |
| CPU | 21 s com teto de 1 núcleo, 42 s com 2 |
| Memória de pico | 423–456 MB dirigindo o Chrome por CDP; 553–601 MB pelo Playwright; 650–752 MB antes das flags de download |
| Download | 2,4–4,8 MB por execução; 49–54 MB antes das flags |
| Upload | ~1 MB |
| Imagem | 1,79 GB |

Distribuição do tempo numa execução de 33 s: captcha 11–14 s, autorização
6,5 s, CPF 4,3 s, sessão da API 2,2–2,9 s, POSTs HTTP ~2 s. É uma carga presa
em latência, com rajadas de CPU no Chromium.

## Onde a memória está

PSS por processo, execução real de 30 s: renderers 311 MB (portal, iframe do
hCaptcha e SSO em processos separados por isolamento de origem), processo do
navegador 157 MB, driver Node do Playwright 121 MB, utility 87 MB, GPU 74 MB,
Xvfb 45 MB, zygote 29 MB, Python 43 MB, mais o perfil em tmpfs cobrado no
cgroup. Somar RSS engana: conta página compartilhada uma vez por processo e
infla o total em três vezes. A régua aqui é `memory.stat` do cgroup (campos
`anon`, `file`, `shmem`) mais `smaps_rollup` por processo.

## O que não funcionou

Quatorze variantes de flag, cada uma num login completo, moveram a memória
anônima do cgroup entre 481 e 524 MB — nenhuma ganhou: `--renderer-process-limit=1`,
`--disable-features=SitePerProcess,SpareRendererForSitePerProcess,BackForwardCache`,
`--disable-component-extensions-with-background-pages`,
`--js-flags=--max-semi-space-size=1,--max-old-space-size=256`, `--disable-gpu`,
janela menor, perfil fora do tmpfs e headless. Quando uma tira de um lugar, a
memória reaparece em outro: `--disable-gpu` derruba o processo de GPU de 74 para
31 MB e o anônimo sobe. Headless também está fora por outro motivo: o desafio
não resolve. O Chromium é cego ao cgroup no Linux, então ele não se auto-poda —
não adianta confiar no limite do container como sinal de pressão.

### O que a fonte do Chromium já respondeu

Leitura do tree (nov/2026) mais medições de PSS no pico, base de 578 MB:

- `--enable-low-end-device-mode` não devolve memória. Ele troca os tetos de heap
  (`gin/isolate_holder.cc` passa a RAM falsa ao V8, derrubando o old space de
  4 GiB para 256 MiB) e faz `AmountOfTotalPhysicalMemory()` virar
  `min(512 MiB, real)`, mas o que a página consome são dezenas de MB: nenhum MB
  de RSS sai. O preço é fingerprint — `navigator.deviceMemory` cai de 8 para 0.5
  e `performance.memory.jsHeapSizeLimit` de ~4,29e9 para ~2,68e8.
- Pressão de memória não chega. `CreateMemoryPressureMonitor` existe no Linux,
  mas `CreateDefaultSystemEvaluator` devolve `nullptr` fora de
  Windows/Mac/ChromeOS, então não há produtor de sinal: a política de 100/50/0%
  fica em 100% e nada purga cache neste login. `--memory-pressure-off` também
  não existe mais no tree.
- `--in-process-gpu`: 589 MB de PSS no pico contra 578 MB da base, com o login
  aceito. Pior, descartado.
- `--js-flags=--max-old-space-size=192`: 588 MB de pico. Janela de 900x700 em
  vez de 1280x900: 593 MB. Ambos aceitos no POST do captcha e ambos sem ganho.

### Qual régua cada número usa

As cifras deste documento são **memória anônima do cgroup**, a que um
`docker run -m` enxerga: o Chrome, o Xvfb e o Python somados. Medido agora, na
mesma execução, o pico é de 342 MB anônimos contra 582 MB de **PSS total** — a
diferença é página file-backed das bibliotecas do Chrome (libchrome, fontes,
ICU), que é compartilhada e não cresce com o uso. Comparar números de réguas
diferentes produz uma regressão que não existe: 342 MB anônimos hoje contra os
423–456 MB registrados em #30 estão na mesma faixa, já que aquela medição é da
mesma régua e o total é o que pesa mais.

A conta fecha com a distribuição do pico: renderers somam 707 MB de RSS (197 e
179 MB das páginas do SSO, 138 MB do próprio UI do Chrome em
`--top-chrome-webui`), utility 320 MB (109 MB no serviço de rede), browser
212 MB e GPU 146 MB. O peso é a página do SSO somada à UI do navegador, não um
processo que dê para enxugar por flag.

## O que funcionou: parar os downloads de fundo

Um Chrome parado em `about:blank` baixa 42–50 MB em 30 s: 40 requisições a
`optimizationguide-pa.googleapis.com/downloads`, o dicionário de corretor
`en-us-10-2.bdic`, o seed de variations e o serviço de update. Nada disso tem a
ver com o login (a página move 0,8 MB), e o que é baixado também é carregado em
memória. Desligar esses caminhos em `src/captcha/browser.py` levou o download de
49–54 MB para 2,4–4,8 MB e o pico de 650–752 MB para 553–601 MB, com os mesmos
logins passando. `tests/test_browser_flags.py` trava a lista e garante que o
navegador continua headed.

## Como medir

Na branch `diag/captcha-run-lab`: `deploy/actions/mem_sample.py` amostra PSS por
processo e `memory.stat`; `deploy/actions/idle_net.py` mede o download de um
navegador ocioso; `deploy/actions/hosts_probe.py` lista hosts e bytes de uma
página; `INSS_NET_ACCOUNT=1` grava `net_bytes` por host no recibo.

## O driver do Playwright custa 121 MB

O Playwright Python não fala CDP: ele sobe um processo Node que faz a ponte e
que responde por 121 MB de um pico de ~600 MB. Dirigir o Chrome pelo cliente em
`src/captcha/cdp_page.py` remove essa camada: mediana de **444 MB** contra 565 MB
do Playwright em 12 execuções na VPS, e 423–456 MB depois do endurecimento, em 8
execuções, com o login mais rápido (24–29 s contra 26–39 s). O caminho Playwright
continua disponível com `INSS_PAGE_ENGINE=playwright`.

O mesmo cliente economiza CPU em cada captura: decodificar o base64 do
`screenshot` validando UTF-8 custava 479 ms por frame de 665 KB, contra 16 ms
com `skip_utf8_validation=True`.
