# Cliente CDP de página (substitui a API de página do Playwright)

`src/captcha/cdp_page.py` fala CDP direto pelo websocket do Chrome
(`websocket-client`) e reproduz os métodos de página que o fluxo de login usa.
O driver Node do Playwright deixa de existir — a economia medida na VPS é o
processo de driver (121 MB PSS), sem tocar no Chrome, no Xvfb nem no fluxo de
produção.

`src/captcha/browser.py` continua igual: Xvfb + Chrome headed com
`--remote-debugging-port`. O que muda é só quem conversa com o Chrome.

## Anexar

```python
from captcha import cdp_page

browser = cdp_page.connect(manager.start(wait_ready=True))  # aceita http://127.0.0.1:PORT
page = browser.contexts[0].pages[0]                          # mesmo caminho do Playwright
browser.version                                              # string do /json/version
browser.close()                                              # fecha só o websocket
```

Dependência nova: `websocket-client` (testado com 1.8.0). Nada mais.

## Trocar na VPS (não feito aqui)

Em `src/inss/http_login.py`:

```python
-from playwright.sync_api import Error as PlaywrightError, sync_playwright
+from captcha import cdp_page
+PlaywrightError = cdp_page.Error          # mesmos pontos de except, mesmo __name__

-    pw = resources.enter_context(sync_playwright())
     ...
-    browser = pw.chromium.connect_over_cdp(manager.start(wait_ready=True))
+    browser = cdp_page.connect(manager.start(wait_ready=True))
```

`src/inss/session.py`, `src/captcha/aria.py` e `src/comprasnet/workflow.py` só
usam `PlaywrightError` para classificar falha de página; trocar o import pela
mesma alias resolve. `src/inss/workflow.py` (legado) não é usado pelo caminho
`inss-login-http`.

Depois disso `playwright` pode sair das dependências de runtime (mantenha-o só
para o teste, que usa o Chromium que ele já instalou).

## Validar na VPS

1. `pip install websocket-client`
2. Teste de API contra o Chromium já instalado, headed sob Xvfb (o mesmo que o
   fluxo usa):

   ```bash
   CAPY_CDP_HEADED=1 DISPLAY=:97 \
   CAPY_CDP_CHROMIUM=$HOME/.cache/ms-playwright/chromium-*/chrome-linux64/chrome \
   PYTHONPATH=src python -m pytest tests/test_cdp_page.py -q
   ```

   Sem `CAPY_CDP_CHROMIUM` o teste procura o binário em
   `~/.cache/ms-playwright`; sem `CAPY_CDP_HEADED` ele sobe headless.
3. Rodar o fluxo real (`inss-login-http`) e comparar o `receipt.json` com uma
   execução anterior: `success`, `phase_ms`, `browser_version`, `rounds`,
   `captcha_http_status`, `token_age_at_http_post_ms`.
4. Medir de novo: `ps -eo pid,rss,args | grep -i playwright` não deve achar
   nada, e o PSS por tipo deve perder a linha do driver Node.

Pontos que dependem do Chrome headed de verdade e que só a VPS prova: o POST do
SSO sendo cumprido por `Fetch.fulfillRequest` no meio de uma navegação, o
`wait_for_selector` do marcador `[data-auth-handoff]` logo depois, e o
`screenshot` com clip do modal do hCaptcha (posição/DPI da janela).

## Tabela de equivalência

Uso no repositório → comando CDP → o que muda.

### Anexar e encerrar

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `sync_playwright()` + `chromium.connect_over_cdp(url)` (`inss/http_login.py:224`, `inss/workflow.py:119`) | `GET /json/version` → websocket → `Target.getTargets` + `Target.attachToTarget(flatten=true)` | Sem driver Node. `browser.contexts` tem sempre um contexto (o Chrome sem perfil não expõe mais que isso). `browser.close()` fecha só o websocket, não mata o Chrome — igual ao Playwright. |
| `browser.contexts[0].pages[0]` | `Target.getTargets` a cada acesso | Lista os alvos na hora; cada `page` recebe uma sessão plana própria. |
| `browser.version` (`http_login.py:229`) | `/json/version`.Browser | Mesmo texto. |
| `page.context.cookies()` (`auth/browser_captcha.py:66`) | `Storage.getCookies` | Mesmos campos; cookie de sessão com `expires=-1`; `sameSite` cru do CDP. |

### Navegação e estado

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.goto(url, wait_until=..., timeout=...)` (`inss/session.py:163`, `comprasnet/workflow.py:60`) | `Page.navigate` + `Page.setLifecycleEventsEnabled` + evento `Page.lifecycleEvent` | Espera o evento do `loaderId` devolvido pelo `navigate`; `networkidle` é o evento do Chrome, não o contador de conexões do Playwright. Retorna `NavigationResponse(url, loader_id)` sem status HTTP (o Playwright devolve `Response`). Falha de rede vira `Error` com o `errorText`. |
| `page.url` (`session.py:45,166,204`; `http_login.py:315`) | estado de `Page.frameNavigated` + `Page.navigatedWithinDocument` | Mesmo mecanismo (sem polling): uma navegação ainda não commitada mostra a URL anterior. |
| `page.wait_for_url(predicado, timeout)` (`session.py:215`, `comprasnet:62`) | polling de `page.url` a cada 50 ms | Aceita função, string exata ou glob; resolução até 50 ms mais lenta. |
| `page.content()` (`http_login.py:316`) | `Runtime.evaluate` (doctype + `outerHTML`) | Byte-idêntico ao Playwright na comparação feita aqui. |
| `page.wait_for_timeout(ms)` (32 usos) | `time.sleep` | Mesmo tempo de parede. No Playwright essa chamada bombeava o driver; aqui os eventos já correm em threads próprias. |

### JS e DOM

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.evaluate(js)` / `page.evaluate(js, arg)` (`hcaptcha.py:37,43,48`; `session.py:154,228`; `browser_captcha.py:27,70,71`) | `Runtime.evaluate` (`awaitPromise`, `returnByValue`, `userGesture`) | Só valores JSON: não devolve `JSHandle`, nó DOM, `Date`, `Map`. `undefined` → `None`; `NaN`/`±Infinity`/`-0`/bigint vêm por `unserializableValue`. `arg` só é aplicado quando a expressão é função (igual ao Playwright). Exceção de JS vira `Error` com a descrição do erro. |
| `page.query_selector(sel)` (`http_login.py:309,317`) | `Runtime.evaluate` de `document.querySelectorAll` + `Runtime.callFunctionOn` para o índice | Handle ou `None`; sem auto-wait (igual). Métodos do handle: `click`, `is_visible`, `is_enabled`, `inner_text`, `text_content`, `input_value`, `get_attribute`, `bounding_box`, `evaluate`, `query_selector(_all)`, `release`. |

### Esperas

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.wait_for_selector(sel, timeout=...)` (`session.py:165`; `http_login.py:328`) | polling de `Runtime.evaluate` a cada 50 ms | Até 50 ms de atraso extra em relação ao observer de mutação do Playwright. `state` default `visible`, como no Playwright; devolve o handle que satisfez a espera (`None` em `hidden`/`detached`). O polling atravessa troca de documento e de frame e `timeout=0` significa sem prazo. Timeout levanta `cdp_page.TimeoutError`. |

### Entrada

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.mouse.move(x, y, steps=N)` (`hcaptcha.py:53`; `interactions.py:88`; `session.py:139,189,199`) | N × `Input.dispatchMouseEvent(mouseMoved)` | Mesma interpolação; parte de (0,0) como o Playwright; sem pausa entre passos. |
| `page.mouse.click(x, y)` (`hcaptcha.py:55`; `interactions.py:90`; `session.py:191,201`) | `mouseMoved` + `mousePressed` + `mouseReleased` (`button=left`, `buttons=1`, `clickCount=1`) | Mesmo conjunto de eventos; sem a espera de "página ocupada" que o Playwright faz antes de clicar. |
| `page.keyboard.type(ch, delay=...)` (`session.py:145`) | `Input.dispatchKeyEvent(keyDown, text=ch)` + `keyUp` por caractere | Não modela modificadores nem teclas especiais; para texto em `input` o valor final é idêntico (testado). `keyboard.press` cobre Enter/Tab/Escape. |

### Screenshot

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.screenshot(path=, clip=, timeout=)` (`hcaptcha.py:74,225`; `interactions.py:102`; `workflow.py:216`) | `Page.captureScreenshot` com `clip` | `clip` em px de viewport, **igual ao Playwright** (confirmado contra ele: com a página rolada em 500, `clip y=0` mostra o documento em y=500). O módulo soma o scroll e captura limitado ao viewport. Clip totalmente fora do viewport levanta `Error`; parcialmente fora é recortado em silêncio (o Playwright levanta erro). Sempre devolve bytes e grava `path` quando informado. `full_page=True` usa `Page.getLayoutMetrics`. |

### Seletores e locators

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.locator(sel)` + `.first` / `.last` / `.nth` (`session.py:137,174,180,197`; `aria.py:46`) | `Runtime.evaluate` (array de nós) + `Runtime.callFunctionOn` (índice) | Sem strict mode: resolvedor com vários nós usa o índice (0 por default) em vez de levantar. `:visible` e `:has-text("...")` emulados por filtro JS. Sem `>>`, sem shadow DOM, sem `:nth-match`. |
| `locator.bounding_box(timeout)` (`session.py:138,188,198`; `aria.py:104`) | `Runtime.callFunctionOn(getBoundingClientRect)` (+ `DOM.getFrameOwner`/`DOM.getBoxModel` para somar o offset de iframes) | Espera só "attached". Coordenadas no viewport do frame principal, inclusive dentro de iframe (verificado contra o Playwright: mesmo x/y). Sem scroll automático. |
| `locator.click(timeout)` (`session.py:144`; `aria.py`) | `scrollIntoView` + centro + `Input.dispatchMouseEvent` | Espera visível e habilitado (como o Playwright); não espera estabilidade nem hit-target. |
| `locator.input_value/is_visible/is_enabled/all_inner_texts/wait_for(state)` (`session.py:174,182,184`; `aria`) | `Runtime.callFunctionOn` / `Runtime.evaluate` | `is_enabled()` devolve `False` quando o elemento ainda não existe (o Playwright espera e estoura); é o que o loop de `submit_cpf` precisa. `wait_for` faz polling de 50 ms compartilhando um único deadline com a resolução do context do frame: uma espera de 600 ms não vira 10 s enquanto o iframe carrega. |
| `locator.element_handles()` + `handle.evaluate(js)` (`aria.py:48,70`) | `Runtime.evaluate` do array + `Runtime.callFunctionOn` por item | Handle é um `RemoteObject` do CDP; `evaluate` chama a função com `this` = elemento e o elemento como 1º argumento. |
| `page.get_by_role(role, name=, exact=)` (`session.py:59,212`; `comprasnet:61`) | `Runtime.evaluate` com cálculo de nome acessível em JS | Papéis de uma tabela fixa (`button`, `heading`, `link`, `textbox`, `checkbox`, `radio`, `img`, `listitem`, `paragraph`) mais `[role="..."]`. Nome = `aria-labelledby` → `aria-label` → `value` → `innerText`. Não é o algoritmo ARIA completo. |
| `page.get_by_text(text, exact=)` (`session.py:211`) | `Runtime.evaluate` sobre `body *` com `innerText` normalizado | `exact` compara o texto inteiro do elemento; o Playwright escolhe o menor elemento que casa. |

### Eventos de rede

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.on("request", h)` / `remove_listener` (`http_login.py:245`; `prefetch.py:106,217`) | `Network.enable` + `Network.requestWillBeSent` | Handler roda na thread do dispatcher, não na thread que chamou; exceção de handler é logada e não propaga. `headers` com chave minúscula. POST interceptado por `route` continua emitindo o evento com `post_data` (testado). Mapa `Request` por `requestId` limitado a 2048 entradas. |
| `page.on("response", h)` (`prefetch.py:107`) | `Network.responseReceived` | `response.request` é exatamente o mesmo objeto emitido em `on("request")` — o `prefetch` usa isso como chave de dicionário. `body()` usa `Network.getResponseBody`, que falha se o Chrome já descartou o recurso; o Playwright guarda o próprio buffer. Para o `prefetch` conseguir ler as imagens, o módulo habilita `Network.enable` com 20 MB de buffer total e 8 MB por recurso — o mesmo dado que o Playwright acumulava no processo driver. |

### Interceptação

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.route(glob, handler)` (`http_login.py:249`) | `Fetch.enable(patterns)` + evento `Fetch.requestPaused` | `route.continue_()` = `Fetch.continueRequest`, `route.abort()` = `Fetch.failRequest` (`failed`→`Failed`, `aborted`→`Aborted`, etc.), `route.fulfill()` = `Fetch.fulfillRequest`. O handler precisa resolver exatamente uma vez, senão a request fica pausada — mesma semântica do Playwright. Sem `route.fallback`/`route.fetch`. Handler roda no dispatcher. Glob vira padrão CDP mais largo + filtro no cliente. |
| `route.request.url/method/post_data/headers` (`auth/handoff.py:28-57`) | `Fetch.requestPaused.request` | `post_data` presente no POST (testado), inclusive `content-type`. |
| `page.unroute` | novo `Fetch.enable` ou `Fetch.disable` | Não usado no repositório. |

### Frames e init script

| Playwright (uso) | CDP | Muda |
| --- | --- | --- |
| `page.frames` / `frame.locator(...).count()` / `element_handles()` (`aria.py:45-48`) | `Page.getFrameTree` + `Page.frameAttached/frameNavigated/frameDetached` + `Runtime.executionContextCreated` | Cada frame avalia no próprio execution context. Frames filhos são descartados quando o documento principal troca (o Chrome não manda `frameDetached` nesse caso). |
| `page.add_init_script` (não usado no repositório) | `Page.addScriptToEvaluateOnNewDocument` | Funções são embrulhadas em `(fn)()`; o identificador é devolvido para `remove_init_script`. |

### Fora de escopo

Não implementado porque o fluxo não usa: `wait_for_load_state`, `reload`,
`go_back/go_forward`, `set_extra_http_headers`, `add_cookies`,
`expect_*`, `pdf`, `select_option`, `check/uncheck`, `frame_locator`,
`locator.fill/filter/has_text`, `route.fetch/fallback`, diálogos, downloads,
tracing e vídeo.

## O que foi verificado nesta máquina

`tests/test_cdp_page.py` sobe o Chromium do Playwright (por default headless)
com `--remote-debugging-port` e exercita o cliente contra um servidor HTTP
local: navegação com `domcontentloaded`/`load`, `evaluate` com e sem argumento,
`query_selector` presente/ausente, `wait_for_selector` com timeout,
`locator` (first/last, bounding_box, input_value, is_visible/is_enabled,
all_inner_texts, wait_for), `get_by_role`/`get_by_text`, teclado, clique por
coordenada, `screenshot` com clip em viewport rolado, `route` abort/fulfill/
continue (inclusive navegação, POST e o padrão do handoff), `on("request")`/
`on("response")` com `body()`, frames com elemento e offset de iframe,
`add_init_script`, cookies e `wait_for_url`. 19 testes.

Comparações pontuais contra o Playwright na mesma página (scripts descartáveis,
não versionados): `content()` idêntico, eventos de `request` idênticos
(método, URL, `post_data`, headers), `bounding_box` igual no frame principal e
dentro de iframe, e a semântica de `clip` do `screenshot` confirmada como
viewport-relativa.

O que este ambiente não prova: Chrome headed sob Xvfb, DPI/janela reais, o POST
real do SSO e o gasto de memória. Isso fica para a execução na VPS.
