"""One GOV.BR/Meu INSS login attempt on native Android; no captcha solver."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import random
import re
import time

from experiments.android_cdp.client import CommandError, DirectChrome

AUTHZ = ("https://sso.acesso.gov.br/authorize?response_type=code"
         "&client_id=autorizar.meu.inss.gov.br"
         "&scope=openid+email+profile+govbr_confiabilidades_idtoken"
         "+poav1_createPoaAccessHistory_agent"
         "+poav1_retrievePowersOfAttorneyByAgentAccountIdAndGrantorAccountIdAndClientIdAndIsActive_agent"
         "&redirect_uri=https%3A%2F%2Fmeu.inss.gov.br%2F"
         "&nonce={nonce}&state={state}")

STATE = r"""(() => {
 const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();
   return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight&&getComputedStyle(e).visibility!=='hidden'};
 const text=document.body?.innerText||'';
 const frames=[...document.querySelectorAll('iframe')].filter(f=>{
   const r=f.getBoundingClientRect();let e=f;
   while(e){const s=getComputedStyle(e);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;e=e.parentElement;}
   return visible(f)&&r.width>200&&r.height>150&&/hcaptcha|recaptcha/.test(f.src)});
 let tokens=false;
 if(location.hostname==='meu.inss.gov.br')tokens=!!(localStorage.getItem('ifs_auth')&&localStorage.getItem('miToken'));
 return {host:location.hostname,https:location.protocol==='https:',ready:document.readyState,
   account_field:visible(document.querySelector('input[name="accountId"],input#accountId')),
   password_field:visible(document.querySelector('input[type="password"]')),
   visual_captcha:frames.length>0,
   invalid_credentials:/ERL0003900|usuário e\/ou senha inválidos/i.test(text),
   provider_codes:[...new Set(text.match(/\bERL\d+\b/g)||[])],
   two_factor:visible(document.querySelector('input#otpInput[name="otpInput"]'))&&/Verificação em duas etapas/i.test(text),
   consent_meu_inss:location.hostname==='sso.acesso.gov.br'&&/Serviço:\s*Meu INSS\s*-\s*Site/.test(text),
   session_tokens_present:tokens,
   rejected:/requested URL was rejected|access denied|acesso negado/i.test(text)};
})()"""


def classify(state, visual_seconds=0):
    if state.get("invalid_credentials"):
        return "invalid_credentials"
    if state.get("two_factor"):
        return "two_factor_required"
    if state.get("visual_captcha") and visual_seconds >= 3:
        return "visual_captcha_required"
    if state.get("rejected"):
        return "portal_rejected"
    if state.get("host") == "meu.inss.gov.br" and state.get("session_tokens_present"):
        return "verify_session"
    return None


def press(client, labels):
    encoded = json.dumps(labels)
    client.evaluate("""(() => {document.activeElement?.blur();const b=[...document.querySelectorAll('button,input[type="submit"]')]
      .find(e=>""" + encoded + """.includes((e.textContent||e.value||'').trim())&&e.offsetParent);
      if(!b)throw Error('Expected login button missing');b.scrollIntoView({block:'center',behavior:'instant'});})()""")
    client.loop.run_until_complete(asyncio.sleep(0.3))
    point = client.evaluate("""(() => {const b=[...document.querySelectorAll('button,input[type="submit"]')]
      .find(e=>""" + encoded + """.includes((e.textContent||e.value||'').trim())&&e.offsetParent);
      const r=b.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
      return {x,y,covered:!b.contains(document.elementFromPoint(x,y)),disabled:!!b.disabled};})()""")
    assert not point.pop("covered") and not point.pop("disabled"), "Login button unavailable"
    client.call("Input.synthesizeTapGesture", {**point, "gestureSourceType": "touch"})


def enter(client, selector, value):
    state = client.evaluate("({host:location.hostname,https:location.protocol==='https:'})")
    assert state == {"host": "sso.acesso.gov.br", "https": True}, "Credentials require exact GOV.BR HTTPS origin"
    client.clear(selector)
    client.press_keys(selector, value)
    if 'password' in selector:
        expression = "document.querySelector(" + json.dumps(selector) + ").value===" + json.dumps(value)
    else:
        expression = "document.querySelector(" + json.dumps(selector) + ").value.replace(/\\D/g,'')===" + json.dumps(value)
    assert client.evaluate(expression) is True, "Typed login field mismatch"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cpf = re.sub(r"\D", "", os.environ["INSS_TEST_CPF"])
    password = os.environ["INSS_TEST_PASSWORD"]
    assert len(cpf) == 11 and password
    report = {"status": "running", "stage": "connect", "solver_enabled": False,
              "password_submissions": 0, "cpf_submissions": 0, "visual_captcha_seen": False,
              "authentication_verified": False, "personal_data_extracted": False}
    client = None
    started = time.monotonic()
    try:
        client = DirectChrome(host="127.0.0.1", port=9229)
        browser = client.call("Browser.getVersion")
        assert "Android" in browser["userAgent"], "Native Android required"
        report["browser"] = browser["product"]
        client.call("Page.enable")
        client.call("Page.navigate", {"url": AUTHZ.format(nonce=random.random()*10, state=random.random()*10)})
        report["stage"] = "login"
        visual_since = None
        consent_sent = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                state = client.evaluate(STATE)
            except CommandError as error:
                if error.code == -32000:
                    client.loop.run_until_complete(asyncio.sleep(.2))
                    continue
                raise
            report["last_state"] = state
            if state["visual_captcha"]:
                report["visual_captcha_seen"] = True
                visual_since = visual_since or time.monotonic()
            else:
                visual_since = None
            decision = classify(state, time.monotonic()-visual_since if visual_since else 0)
            if decision == "verify_session":
                report["stage"] = "verify_authenticated_session"
                expression = """(async()=>{const r=await fetch('https://vip-pmeuinss-api.inss.gov.br/apis/usuarioservices/info/' + """ + json.dumps(cpf) + """ + '?refreshPedidos=false',{
                  headers:{Authorization:'Bearer '+localStorage.getItem('ifs_auth'),mitoken:localStorage.getItem('miToken')},
                  signal:AbortSignal.timeout(15000)});return r.status;})()"""
                status = client.loop.run_until_complete(client.page.evaluate(expression, await_promise=True))
                report["authenticated_probe_http"] = status
                report["authentication_verified"] = status == 200
                report["status"] = "success" if status == 200 else "session_check_failed"
                break
            if decision:
                report["status"] = decision
                break
            if state["https"] and state["host"] == "sso.acesso.gov.br":
                if state["password_field"] and report["password_submissions"] == 0:
                    report["stage"] = "password_submit"
                    enter(client, 'input[type="password"]', password)
                    press(client, ["Continuar", "Entrar"])
                    report["password_submissions"] = 1
                elif state["account_field"] and report["cpf_submissions"] == 0:
                    report["stage"] = "cpf_submit"
                    enter(client, 'input[name="accountId"],input#accountId', cpf)
                    press(client, ["Continuar"])
                    report["cpf_submissions"] = 1
                elif state["consent_meu_inss"] and not consent_sent:
                    press(client, ["Autorizar"])
                    report["meu_inss_consent_authorized"] = True
                    consent_sent = True
            client.loop.run_until_complete(asyncio.sleep(.3))
        else:
            report["status"] = "timeout"
    except Exception as error:
        report.update(status="error", error_type=type(error).__name__, cdp_method=getattr(error,"method",None), code=getattr(error,"code",None))
    finally:
        report["elapsed_s"] = round(time.monotonic()-started,2)
        if client is not None:
            report["cdp_commands"] = dict(client.commands)
            client.quit()
        serialized = json.dumps(report,indent=2)
        assert cpf not in serialized and password not in serialized
        args.output.write_text(serialized+'\n')
        print(serialized,flush=True)
    return int(report["status"]!='success')


if __name__ == "__main__":
    raise SystemExit(main())
