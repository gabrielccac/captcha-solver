"""Offline trust-boundary, queue, ownership, renewal and MCP transport tests."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

from cryptography.fernet import Fernet
from mcp import types
import pytest
import requests
from starlette.testclient import TestClient

from inss import hosted
from inss.hosted_catalog import redact_tokens, resolve
from inss.hosted_store import ServiceError, Store
from inss.hosted_worker import freeze, thaw


def jwt(**claims):
    return 'fixture.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.fixture'


def bundle(exp=None):
    expiry = time.time()+3600 if exp is None else exp
    return {'cpf':'01234567890', 'headers': {'Authorization':'Bearer '+jwt(sub='01234567890', exp=expiry, iat=expiry-3600),
        'miToken': jwt(exp=expiry, iat=expiry-3600, nbs=['123'], nbsJson={'listaBeneficios':[
            {'numero':'123','codigoEspecie':31}]}), 'User-Agent':'fixture-real-UA', 'sessionId':'fixture'}, 'cookies':[]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Offline hosted tests cannot use the network')
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)


@pytest.fixture
def service(tmp_path):
    config = hosted.Config(str(tmp_path/'state.db'), Fernet.generate_key().decode(), 'fixture-vision-key',
        {'alpha': {'token':'a'*40, 'export_credentials':True},
         'bravo': {'token':'b'*40, 'export_credentials':False}}, 'http://testserver')
    return hosted.Service(config)


def add_session(service, owner='alpha', value=None):
    job = service.enqueue(owner, '01234567890', 'fixture-password')
    claimed = service.store.claim()
    assert claimed[0]==job['job_id']
    service.store.finish(claimed[0], owner, bundle=value or bundle())
    return service.store.job(owner, claimed[0])['session_id']


def test_encryption_and_single_global_browser_claim(service):
    first = service.enqueue('alpha','01234567890','fixture-password','fixture-key')
    assert service.enqueue('alpha','01234567890','fixture-password','fixture-key')['job_id']==first['job_id']
    service.enqueue('bravo','01234567890','fixture-password')
    peer = Store(service.config.database, service.config.encryption_key)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda store: store.claim(), [service.store, peer]))
    assert sum(claim is not None for claim in claims)==1
    assert b'fixture-password' not in service.store.path.read_bytes()
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT payload FROM jobs WHERE state='running'").fetchone()[0] is None
    job_id, owner, _ = next(claim for claim in claims if claim)
    peer.finish(job_id, owner, code='two_factor_required')
    assert service.store.claim() is not None


def test_queue_limit_and_expired_jobs_clear_password_payload(service):
    service.store.queue_limit = 1
    queued = service.enqueue('alpha','01234567890','fixture-password')
    with pytest.raises(ServiceError, match='login_queue_full'):
        service.enqueue('bravo','01234567890','fixture-password')
    with service.store.db() as db:
        db.execute('UPDATE jobs SET deadline=0')
    assert service.store.claim() is None
    assert service.store.job('alpha',queued['job_id'])['code']=='job_expired'
    with service.store.db() as db:
        assert db.execute('SELECT payload FROM jobs').fetchone()[0] is None


def test_cookie_roundtrip_preserves_scope_and_flags():
    with requests.Session() as session:
        session.headers.update(bundle()['headers'])
        session.cookies.set('sso-fixture','fixture-secret',domain='sso.acesso.gov.br',path='/authorize',
                            secure=True,rest={'HttpOnly':True})
        with thaw(freeze(session, '01234567890')) as restored:
            cookie = next(iter(restored.cookies))
            assert (cookie.domain,cookie.path,cookie.secure)==('sso.acesso.gov.br','/authorize',True)
            assert cookie.has_nonstandard_attr('HttpOnly')
            assert restored.headers['User-Agent']=='fixture-real-UA'


@pytest.mark.parametrize('path,query', [
    ('https://example.com/',{}), ('//example.com/',{}), ('/../govbrservices/token/x',{}),
    ('/usuarioservices/info/99999999999',{}), ('/usuarioservices/info/{cpf}',{'refreshPedidos':'true'}),
    ('/calculoBeneficioServices/concalPdf/999',{}),
    ('/hiscreServices/historicocreditos/{cpf}/31-02-2026/01-03-2026',{}),
    ('/hiscreServices/historicocreditos/{cpf}/01-01-2020/01-01-2025',{}),
])
def test_catalog_rejects_arbitrary_urls_ids_dates_and_query(path,query):
    with pytest.raises(ServiceError):
        resolve(path,query,bundle())


def test_catalog_supplies_own_cpf_and_redacts_nested_tokens():
    path, query, _ = resolve('/usuarioservices/info/{cpf}',{},bundle())
    assert path.endswith('/01234567890') and query=={'refreshPedidos':'false'}
    assert redact_tokens({'miToken':'secret','child':[{'access_token':'secret','name':'ok'}]})=={'child':[{'name':'ok'}]}
    assert redact_tokens({'opaque':'prefix private-token'},('private-token',))=={'opaque':'prefix [redacted]'}


def test_parallel_expired_requests_renew_once_and_then_fetch_in_parallel(service,monkeypatch):
    session_id = add_session(service, value=bundle(time.time()-1))
    calls = []
    def renewal(old):
        calls.append(1)
        time.sleep(.04)
        return bundle(), {'success':True}
    active, peak = 0, 0
    def request(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak,active)
        time.sleep(.04)
        active -= 1
        return {'upstream_status':200,'data':{'ok':True}}
    monkeypatch.setattr(hosted,'run_renewal',renewal)
    monkeypatch.setattr(hosted,'fetch',request)
    async def run():
        return await asyncio.gather(*(service.request('alpha',session_id,'/usuarioservices/info/{cpf}',{}) for _ in range(4)))
    assert len(asyncio.run(run()))==4
    assert len(calls)==1 and peak>1


def test_renewal_failure_preserves_old_session_and_never_falls_back_to_login(service,monkeypatch):
    session_id = add_session(service)
    before = service.store.session('alpha',session_id)
    def fail(*args):
        raise hosted.AuthError('sso_login_required')
    monkeypatch.setattr(hosted,'run_renewal',fail)
    with pytest.raises(ServiceError,match='sso_login_required'):
        asyncio.run(service.renewal('alpha',session_id))
    assert service.store.session('alpha',session_id)==before


def test_queued_worker_finishes_once_and_discards_password(service,monkeypatch):
    queued = service.enqueue('alpha','01234567890','fixture-password')
    seen = []
    async def worker(credentials):
        seen.append(dict(credentials))
        return {'success':True,'bundle':bundle(),'timing':{'elapsed_ms':10}}
    monkeypatch.setattr(service,'login_worker',worker)
    async def run():
        task = asyncio.create_task(service.work())
        for _ in range(100):
            await asyncio.sleep(.005)
            if service.store.job('alpha',queued['job_id'])['status']=='succeeded':
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert len(seen)==1 and seen[0]['password']=='fixture-password'
    assert service.store.job('alpha',queued['job_id'])['status']=='succeeded'
    assert b'fixture-password' not in service.store.path.read_bytes()


def test_failed_claim_is_not_retried_after_worker_loss(service):
    queued = service.enqueue('alpha','01234567890','fixture-password')
    service.store.claim()
    with service.store.db() as db:
        db.execute('UPDATE jobs SET deadline=0')
    assert service.store.claim() is None
    assert service.store.job('alpha',queued['job_id'])['code']=='worker_lost'


def test_fetch_strips_tokens_and_does_not_follow_redirects(service,monkeypatch):
    from unittest.mock import MagicMock
    response = MagicMock(status_code=200)
    response.__enter__.return_value = response
    response.iter_content.return_value = [b'{"miToken":"fixture-private","result":{"access_token":"hidden","ok":true}}']
    get = MagicMock(return_value=response)
    monkeypatch.setattr(requests.sessions.Session,'get',get)
    result = hosted.fetch(bundle(),'/usuarioservices/info/{cpf}',{})
    assert result['data']=={'result':{'ok':True}}
    assert get.call_args.kwargs['allow_redirects'] is False
    assert get.call_args.args[0].endswith('/01234567890')


def test_rejected_path_does_not_trigger_renewal(service,monkeypatch):
    session_id = add_session(service,value=bundle(time.time()-1))
    def denied(*args):
        pytest.fail('Bad paths must not trigger requests or renewal')
    monkeypatch.setattr(hosted,'run_renewal',denied)
    with pytest.raises(ServiceError,match='path_not_allowed'):
        asyncio.run(service.request('alpha',session_id,'https://example.com/',{}))


def test_rest_auth_ownership_idempotency_and_export_capability(service,monkeypatch):
    monkeypatch.setattr(hosted,'fetch',lambda *args: {'upstream_status':200,'data':{'ok':True}})
    app = hosted.create_app(service.config,service=service,start_worker=False)
    auth = {'Authorization':'Bearer '+'a'*40}
    other = {'Authorization':'Bearer '+'b'*40}
    session_id = add_session(service)
    bravo = add_session(service,'bravo')
    with TestClient(app) as client:
        assert client.get('/healthz').status_code==200
        assert client.get('/v1/catalog').status_code==401
        assert client.post('/mcp',json={}).status_code==401
        assert client.get('/v1/catalog',headers=auth).status_code==200
        assert client.get('/v1/sessions/'+session_id,headers=other).status_code==404
        assert client.post('/v1/sessions/'+bravo+'/credentials',headers=other).status_code==403
        exported = client.post('/v1/sessions/'+session_id+'/credentials',headers=auth)
        assert exported.status_code==200 and 'Authorization' in exported.json()['headers']
        assert 'cookies' not in exported.json()
        requested = client.post('/v1/sessions/'+session_id+'/request',headers=auth,
                                json={'path':'/usuarioservices/info/{cpf}'})
        assert requested.status_code==200 and requested.json()['upstream_status']==200, requested.text
        started = client.post('/v1/auth/jobs',headers={**auth,'Idempotency-Key':'fixture-idem'},
                              json={'cpf':'01234567890','password':'fixture-password'})
        again = client.post('/v1/auth/jobs',headers={**auth,'Idempotency-Key':'fixture-idem'},
                            json={'cpf':'01234567890','password':'fixture-password'})
        assert started.status_code==202 and again.json()['job_id']==started.json()['job_id']
        assert client.post('/v1/auth/jobs',headers=auth,content='x'*65537).status_code==413
        assert client.post('/v1/auth/jobs',headers=auth,json={'cpf':'bad','password':'fixture-password'}).json()=={'error':'invalid_credentials_input'}
        assert client.delete('/v1/sessions/'+session_id,headers=auth).status_code==200
        assert client.get('/v1/sessions/'+session_id,headers=auth).status_code==404


def test_mcp_stateless_tools_keep_owner_isolation_and_never_expose_export(service):
    session_id = add_session(service)
    app = hosted.create_app(service.config,service=service,start_worker=False)
    headers = {'Authorization':'Bearer '+'a'*40,'Accept':'application/json, text/event-stream'}
    def rpc(client, method, params=None, other=False):
        return client.post('/mcp',headers={**headers,**({'Authorization':'Bearer '+'b'*40} if other else {})},
            json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    with TestClient(app) as client:
        initialized = rpc(client,'initialize',{'protocolVersion':'2025-03-26','capabilities':{},
            'clientInfo':{'name':'fixture','version':'1'}})
        assert initialized.status_code==200, initialized.text
        listed = rpc(client,'tools/list').json()
        names = {tool['name'] for tool in listed['result']['tools']}
        assert names=={'inss_catalog','inss_auth_start','inss_auth_status','inss_session_status',
                       'inss_session_renew','inss_session_close','inss_request'}
        params = {'name':'inss_session_status','arguments':{'session_id':session_id}}
        good = rpc(client,'tools/call',params)
        assert good.status_code==200, good.text
        assert 'active' in good.text and not good.json()['result'].get('isError',False)
        denied = rpc(client,'tools/call',params,other=True).json()
        assert denied['result']['isError'] and 'session_not_found' in str(denied)
        assert bundle()['headers']['miToken'] not in good.text


def test_pdf_rest_and_mcp_return_binary_without_writing_documents(service,monkeypatch):
    document = b'%PDF-1.4\nfixture-document'
    monkeypatch.setattr(hosted,'fetch',lambda *args: {'upstream_status':200,'content_type':'pdf','body':document})
    session_id = add_session(service)
    headers = {'Authorization':'Bearer '+'a'*40,'Accept':'application/json, text/event-stream'}
    with TestClient(hosted.create_app(service.config,service=service,start_worker=False)) as client:
        response = client.post('/v1/sessions/'+session_id+'/request',headers=headers,
            json={'path':'/calculoBeneficioServices/concalPdf/123'})
        assert response.status_code==200 and response.content==document
        assert response.headers['content-type']=='application/pdf'
        response = client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'inss_request','arguments':{'session_id':session_id,'path':'/calculoBeneficioServices/concalPdf/123'}}})
        result = response.json()['result']
        assert not result.get('isError')
        resource = result['content'][0]['resource']
        assert resource['mimeType']=='application/pdf' and base64.b64decode(resource['blob'])==document


def test_fetch_bounds_response_size_and_rejects_non_pdf(monkeypatch):
    from unittest.mock import MagicMock
    response = MagicMock(status_code=200)
    response.__enter__.return_value = response
    monkeypatch.setattr(requests.sessions.Session,'get',lambda *args,**kwargs: response)
    response.iter_content.return_value = [b'x'*2_000_001]
    with pytest.raises(ServiceError,match='upstream_response_limit'):
        hosted.fetch(bundle(),'/usuarioservices/info/{cpf}',{})
    response.iter_content.return_value = [b'<html>not a PDF</html>']
    with pytest.raises(ServiceError,match='upstream_invalid_pdf'):
        hosted.fetch(bundle(),'/calculoBeneficioServices/concalPdf/123',{})
