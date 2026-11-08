"""Browser bootstrap/CPF/captcha; single-use HTTP handoff, password and API login."""
import argparse
from contextlib import ExitStack, redirect_stderr
import io
import json
from pathlib import Path
import socket
import time
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, quote, urlsplit
import uuid

import requests
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from auth.browser_captcha import adopt
from auth.captcha import NullCaptcha
from auth.govbr import AuthError, GovBrAuth
from auth.handoff import CaptchaHandoff, HANDOFF_SELECTOR, SSO, password_page
from captcha.browser import ManagedBrowser
from captcha import hcaptcha
from . import session as browser_login

PORTAL = "https://meu.inss.gov.br/"
API = "https://vip-pmeuinss-api.inss.gov.br/apis/"
CLIENT = "autorizar.meu.inss.gov.br"
MODEL = "meta/muse-spark-1.3-contributor"


def load_env_file(path):
    """Read literal values, without evaluating shell syntax or interpolating secrets."""
    values = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def verify_api(session, code, cpf, receipt):
    headers = {"Accept": "application/json", "Origin": PORTAL.rstrip("/"),
               "Referer": PORTAL, "userId": uuid.uuid4().hex[:25],
               "sessionId": uuid.uuid4().hex[:25]}
    endpoint = API + f"govbrservices/token/{quote(code, safe='')}/clientid/{CLIENT}"
    response = session.get(endpoint, params={"redirectURL": PORTAL}, headers=headers,
                           timeout=30, allow_redirects=False)
    receipt["code_exchange_http_status"] = response.status_code
    if response.status_code != 200:
        raise AuthError("code_exchange_failed", http_status=response.status_code)
    data = response.json()
    access = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(access, str) or not access:
        raise AuthError("access_token_missing")
    # Identity consistency only; decoding claims is not JWT signature verification.
    if str(browser_login.token_claims(access).get("sub")) != cpf:
        raise AuthError("authenticated_identity_mismatch")
    headers["Authorization"] = "Bearer " + access
    # Match the SPA's initial XHR; replace this literal with the issued token below.
    headers["miToken"] = "undefined"
    info_url = API + f"usuarioservices/info/{cpf}?refreshPedidos=false"
    response = session.get(info_url, headers=headers, timeout=30, allow_redirects=False)
    receipt["userinfo_bootstrap_http_status"] = response.status_code
    if response.status_code != 200:
        raise AuthError("userinfo_bootstrap_failed", http_status=response.status_code)
    data = response.json()
    mi = data.get("miToken") if isinstance(data, dict) else None
    if not isinstance(mi, str) or not mi:
        raise AuthError("mi_token_missing")
    headers["miToken"] = mi
    with session.get(info_url, headers=headers, timeout=30, allow_redirects=False,
                     stream=True) as verified:
        receipt["authenticated_endpoint_http_status"] = verified.status_code
        if verified.status_code != 200:
            raise AuthError("authenticated_endpoint_failed", http_status=verified.status_code)
    # Caller owns this session. Tokens stay in memory for subsequent API requests.
    session.headers.update(headers)
    receipt["api_session_verified"] = True


def renew(session, cpf, *, receipt=None):
    """Renew via retained SSO cookies; caller must serialize access to this session.

    No browser, password, captcha or model key. Commit the new API headers/cookies
    only after a matching OAuth callback, identity check and authenticated HTTP 200.
    """
    receipt = {} if receipt is None else receipt
    receipt.update(success=False, browser_password_posts=0, http_password_posts=0)
    started = time.monotonic()
    try:
        if len(cpf) != 11 or not cpf.isascii() or not cpf.isdigit():
            raise AuthError("invalid_cpf_format")
        ua = session.headers.get("User-Agent")
        if not ua or not any(cookie.domain.lstrip(".") in ("acesso.gov.br", "sso.acesso.gov.br")
                             for cookie in session.cookies):
            raise AuthError("sso_session_missing")
        old_token = session.headers.get("Authorization")
        with requests.Session() as sso, requests.Session() as api:
            # API bearer headers must never be forwarded to the SSO host.
            sso.headers.update({"User-Agent": ua, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
            sso.cookies.update(session.cookies)
            api.headers["User-Agent"] = ua
            scope = parse_qs(urlsplit(browser_login.AUTHZ).query)["scope"][0]
            auth = GovBrAuth(sso, NullCaptcha(), client_id=CLIENT, redirect_uri=PORTAL, scope=scope)
            hops = auth.resume()
            receipt.update(oauth_callback_received=True, redirect_hops=len(hops))
            verify_api(api, auth.authorization_code, cpf, receipt)
            receipt["access_token_changed"] = api.headers.get("Authorization") != old_token
            session.headers.update(api.headers)
            session.cookies.clear()
            session.cookies.update(sso.cookies)
            session.cookies.update(api.cookies)
        receipt.update(success=True, code="ok")
        return receipt
    except AuthError as error:
        receipt["code"] = error.code
        raise
    except requests.RequestException:
        receipt["code"] = "network_error"
        raise AuthError("network_error") from None
    except Exception:
        receipt["code"] = "renewal_runtime_error"
        raise AuthError("renewal_runtime_error") from None
    finally:
        receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000)


def login(session, cpf, password, api_key, *, receipt=None, optimize_interactions=False,
          batch_captcha=False):
    """Populate a caller-owned fresh requests.Session, or raise a sanitized error.

    One login per call, without password/token retries. Chrome and Xvfb close
    before password submission. Callers must close the HTTP session themselves.
    """
    receipt = {} if receipt is None else receipt
    receipt.update(success=False, api_session_verified=False, browser_password_posts=0,
                   http_password_posts=0, http_captcha_posts=0, phase_events=[],
                   model=MODEL, reasoning_effort="minimal")
    receipt.pop("failure_stage", None)
    receipt.pop("failure_http_status", None)
    interactions=None
    if optimize_interactions:
        from captcha.interactions import DynamicInteractions
        interactions=DynamicInteractions(short_clicks=True)
    receipt['interaction_profile']='optimized' if optimize_interactions else 'reference'
    prefetched=None
    started = time.monotonic()

    def phase(name):
        receipt["phase_events"].append({"phase": name,
            "elapsed_ms": round((time.monotonic() - started) * 1000)})

    try:
        if len(cpf) != 11 or not cpf.isascii() or not cpf.isdigit():
            raise AuthError("invalid_cpf_format")
        if not password:
            raise AuthError("password_missing_in_env")
        if not api_key:
            raise AuthError("solver_key_missing")
        if session.headers.get("Authorization") or session.headers.get("miToken") or session.cookies:
            raise AuthError("fresh_http_session_required")
        with ExitStack() as resources:
            phase("browser_start")
            pw = resources.enter_context(sync_playwright())
            profile = resources.enter_context(TemporaryDirectory(prefix="govbr-http-"))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            manager = ManagedBrowser(profile, cdp_port=port, quiet=True)
            resources.callback(manager.stop)
            browser = pw.chromium.connect_over_cdp(manager.start(wait_ready=True))
            resources.callback(browser.close)
            page = browser.contexts[0].pages[0]
            receipt["browser_version"] = browser.version
            vision_session=resources.enter_context(requests.Session())
            if batch_captcha:
                from captcha.prefetch import PrefetchedSolver
                prefetched=PrefetchedSolver(vision_session)
                prefetched.attach(page)
                resources.callback(prefetched.close)
            expected_state = {}

            def track(request):
                parts = urlsplit(request.url)
                if parts.netloc == SSO and parts.path == "/authorize":
                    expected_state["value"] = parse_qs(parts.query).get("state", [None])[0]
                if parts.netloc == SSO and request.method == "POST":
                    fields = parse_qs(request.post_data or "", keep_blank_values=True)
                    if "password" in fields:
                        receipt["browser_password_posts"] += 1

            page.on("request", track)
            phase("authorize")
            browser_login.goto_login(page)
            captured = CaptchaHandoff()
            page.route("https://sso.acesso.gov.br/**", captured.intercept)
            phase("cpf")
            masked=f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
            if optimize_interactions:
                browser_login.submit_cpf(page,masked,short_pauses=True)
            else:
                browser_login.submit_cpf(page,masked)
            phase("captcha_engine")
            with TemporaryDirectory(prefix="captcha-widget-") as images:
                deadline = time.monotonic() + 25
                while not hcaptcha.vis_modal(page) and not hcaptcha.page_state(page, HANDOFF_SELECTOR).get("done"):
                    failure = browser_login.login_failure(page)
                    if failure:
                        raise AuthError(failure.code)
                    if time.monotonic() > deadline:
                        raise AuthError("captcha_challenge_missing")
                    page.wait_for_timeout(500)
                done = False
                for rnd in range(3):
                    if captured.captured_at is not None:
                        break
                    try:
                        with redirect_stderr(io.StringIO()):
                            timing={'interactions':interactions} if interactions is not None else {}
                            if prefetched is not None:
                                timing['vision_solver']=prefetched
                            else:
                                timing['vision_session']=vision_session
                            done, info = hcaptcha.solve_round(page, images, rnd, MODEL,
                                "minimal", HANDOFF_SELECTOR, api_key=api_key, **timing)
                    except PlaywrightError:
                        if captured.captured_at is not None:
                            break
                        raise
                    receipt.setdefault("rounds", []).append({key: info[key] for key in
                        ("round", "modal_wait_s", "capture_s", "extract_s", "solve_s", "click_s", "verdict_s")
                        if isinstance(info.get(key), (int, float))})
                    if interactions is not None:
                        receipt['selection_waits']=list(interactions.metrics)
                    if info.get("why")=="selection_not_confirmed":
                        raise AuthError("captcha_selection_not_confirmed")
                    if "solve_error" in info:
                        if info['solve_error']=='captcha_images_changed':
                            raise AuthError('captcha_images_changed')
                        raise AuthError("solver_provider_error")
                    if captured.captured_at is not None or done:
                        break
            if captured.captured_at is None and not done:
                raise AuthError("captcha_unsolved")
            # A minted token can precede the callback POST; pump browser events first.
            page.wait_for_selector(HANDOFF_SELECTOR, timeout=15000)
            if captured.captured_at is None:
                raise AuthError("captcha_token_not_captured")
            receipt["token_captured_before_browser_send"] = True
            adopt(page, session)
            receipt["token_age_at_http_post_ms"] = round((time.monotonic() - captured.captured_at) * 1000)
            phase("http_consume_captcha")
            receipt["http_captcha_posts"] = 1
            response = captured.consume(session)
            receipt["captcha_http_status"] = response.status_code
            page = password_page(session, response)
            receipt["captcha_http_accepted_password_form"] = True
            auth = GovBrAuth(session, NullCaptcha(), client_id=CLIENT, redirect_uri=PORTAL)
            auth.page = page
            auth.state = expected_state.get("value")
            phase("release_browser")
        receipt["browser_released_at_ms"] = round((time.monotonic() - started) * 1000)
        phase("http_password")
        receipt["http_password_posts"] = 1
        auth.submit_password(password, cpf, captcha_required=False)
        phase("http_oauth_redirect")
        hops = auth.finish()
        receipt["oauth_callback_received"] = True
        receipt["redirect_hops"] = len(hops)
        phase("http_api_session")
        verify_api(session, auth.authorization_code, cpf, receipt)
        receipt.update(success=True, code="ok")
        return receipt
    except AuthError as error:
        events = receipt["phase_events"]
        if events:
            receipt["failure_stage"] = events[-1]["phase"]
        if error.http_status is not None:
            receipt["failure_http_status"] = error.http_status
        raise
    except browser_login.LoginError as error:
        raise AuthError(error.code) from None
    except requests.RequestException:
        raise AuthError("network_error") from None
    except PlaywrightError:
        raise AuthError("browser_error") from None
    except Exception:
        raise AuthError("login_runtime_error") from None
    finally:
        if prefetched is not None:
            receipt['captcha_batch']=prefetched.statistics()
        receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        events = receipt["phase_events"]
        receipt["phase_ms"] = {event["phase"]: (events[i + 1]["elapsed_ms"] if i + 1 < len(events)
            else receipt["elapsed_ms"]) - event["elapsed_ms"] for i, event in enumerate(events)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _: parser.exit(2, "Invalid arguments; credentials belong in the local .env.\n")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--out", type=Path, default=Path("runs/auth-http"))
    parser.add_argument('--fast-interactions',action='store_true',
                        help='Confirm captcha selections visually and shorten interaction pauses')
    parser.add_argument('--batch-captcha',action='store_true',
                        help='Analyze preloaded challenge images together, with per-round fallback')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    receipt = {"success": False, "api_session_verified": False}
    try:
        values = load_env_file(args.env_file)
        cpf = "".join(c for c in values.get("GOVBR_CPF", "") if c in "0123456789")
        with requests.Session() as session:
            login(session, cpf, values.get("GOVBR_PASSWORD", ""),
                  values.get("OPENROUTER_API_KEY", "").strip(), receipt=receipt,
                  optimize_interactions=args.fast_interactions,batch_captcha=args.batch_captcha)
    except (AuthError, browser_login.LoginError) as error:
        receipt["code"] = error.code
    except FileNotFoundError:
        receipt["code"] = "env_file_missing"
    except requests.RequestException:
        receipt["code"] = "network_error"
    except PlaywrightError:
        receipt["code"] = "browser_error"
    except KeyboardInterrupt:
        receipt["code"] = "cancelled"
    except Exception:
        receipt["code"] = "login_runtime_error"
    (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("success", "code")}))
    return 0 if receipt["success"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
