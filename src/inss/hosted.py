"""Authenticated REST + stateless remote MCP sharing encrypted sessions and jobs."""
import asyncio
import base64
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hmac
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from cryptography.fernet import Fernet
from mcp import types
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
import requests
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from auth.govbr import AuthError
from .hosted_catalog import CATALOG, redact_tokens, resolve
from .hosted_store import ServiceError, Store
from .hosted_worker import freeze, thaw
from .http_login import API, load_env_file, renew
from .medical import list_medical_reports
from .session import session_benefits, token_claims, token_validity

OWNER = ContextVar('inss_owner')
MAX_BODY = 65536


@dataclass(repr=False)
class Config:
    database: str
    encryption_key: str = field(repr=False)
    api_key: str = field(repr=False)
    clients: dict = field(repr=False)
    public_url: str = 'http://localhost:8091'

    @classmethod
    def from_file(cls):
        values = load_env_file(os.environ.get('INSS_CONFIG_FILE', '/run/secrets/service.env'))
        config = cls(values['INSS_DATABASE'], values['INSS_ENCRYPTION_KEY'],
                     values['OPENROUTER_API_KEY'], json.loads(values['INSS_CLIENTS']), values['INSS_PUBLIC_URL'])
        Fernet(config.encryption_key)
        if not config.clients or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name)
                or not isinstance(entry.get('token'), str) or len(entry['token'])<32
                or not isinstance(entry.get('export_credentials', False), bool)
                for name, entry in config.clients.items()):
            raise ValueError('invalid_service_config')
        if len({entry['token'] for entry in config.clients.values()})!=len(config.clients):
            raise ValueError('duplicate_client_keys')
        return config


def remaining(bundle):
    headers = bundle['headers']
    return min(float(token_claims(headers['Authorization'].removeprefix('Bearer '))['exp']),
               float(token_claims(headers['miToken'])['exp'])) - time.time()


def run_renewal(bundle):
    receipt = {}
    with thaw(bundle) as session:
        renew(session, bundle['cpf'], receipt=receipt)
        return freeze(session, bundle['cpf']), receipt


def fetch(bundle, path, query):
    path, query, kind = resolve(path, query, bundle)
    with thaw(bundle) as session:
        # Only API headers travel here. SSO cookies are domain-scoped by requests.
        with session.get(API.rstrip('/')+path, params=query, timeout=(5, 30),
                         allow_redirects=False, stream=True) as response:
            result = {'upstream_status': response.status_code, 'content_type': kind}
            if response.status_code != 200:
                return dict(result, error='upstream_http_error')
            chunks, size = [], 0
            deadline = time.monotonic()+90
            limit = 8_000_000 if kind=='pdf' else 2_000_000
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > limit or time.monotonic() > deadline:
                    raise ServiceError('upstream_response_limit', 502)
                chunks.append(chunk)
            body = b''.join(chunks)
            if kind=='pdf':
                if not body.startswith(b'%PDF-'):
                    raise ServiceError('upstream_invalid_pdf', 502)
                return dict(result, body=body)
            try:
                return dict(result, data=redact_tokens(json.loads(body), (
                    bundle['headers']['Authorization'].removeprefix('Bearer '), bundle['headers']['miToken'])))
            except (ValueError, UnicodeError):
                raise ServiceError('upstream_invalid_json', 502) from None


class Service:
    def __init__(self, config, *, store=None):
        self.config = config
        self.store = store or Store(config.database, config.encryption_key)
        self.http_slots = asyncio.Semaphore(8)
        self.session_locks = WeakValueDictionary()

    def enqueue(self, owner, cpf, password, idem=None):
        if (not isinstance(cpf, str) or not re.fullmatch(r'[0-9]{11}|[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}', cpf)
                or not isinstance(password, str) or not 1<=len(password)<=256):
            raise ServiceError('invalid_credentials_input')
        if idem is not None and (not isinstance(idem, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', idem)):
            raise ServiceError('invalid_idempotency_key')
        return self.store.enqueue(owner, {'cpf': re.sub(r'\D', '', cpf), 'password': password}, idem)

    async def login_worker(self, credentials):
        credentials['api_key'] = self.config.api_key
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'inss.hosted_worker',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name!='nt')
        try:
            body = (json.dumps(credentials)+'\n').encode()
            credentials.clear()
            stdout, _ = await asyncio.wait_for(process.communicate(body), timeout=240)
            if process.returncode or len(stdout)>500_000:
                return {'success': False, 'code': 'login_worker_error'}
            return json.loads(stdout)
        except asyncio.TimeoutError:
            return {'success': False, 'code': 'login_timeout'}
        finally:
            credentials.clear()
            # Reap the worker's whole group, including browser descendants, on every exit.
            if os.name!='nt':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                process.kill()
            await process.wait()

    async def work(self):
        while True:
            job = self.store.claim()
            if job is None:
                await asyncio.sleep(.5)
                continue
            job_id, owner, credentials = job
            expected_cpf = credentials['cpf']
            try:
                result = await self.login_worker(credentials)
                bundle = result.get('bundle') if result.get('success') else None
                if bundle is not None:
                    # Reject malformed worker output before storing a usable session.
                    assert bundle['cpf']==expected_cpf
                    assert str(token_claims(bundle['headers']['Authorization'].removeprefix('Bearer '))['sub'])==expected_cpf
                    assert remaining(bundle)>0
                    with thaw(bundle):
                        pass
                self.store.finish(job_id, owner, bundle=bundle,
                    code=result.get('code', 'ok'), timing=result.get('timing'))
            except asyncio.CancelledError:
                self.store.finish(job_id, owner, code='worker_stopped')
                raise
            except Exception:
                self.store.finish(job_id, owner, code='login_worker_error')
            finally:
                credentials.clear()

    async def fresh(self, owner, session_id, *, force=False, expected=None):
        # shortcut: renewal locks are local to the one deployed API process. The DB
        # claim coordinates browsers across processes; use a distributed renewal lock before API replicas.
        lock = self.session_locks.setdefault((owner, session_id), asyncio.Lock())
        async with lock:
            bundle, version, deadline = self.store.session(owner, session_id)
            receipt = None
            if (force and (expected is None or expected==version)) or remaining(bundle)<=30:
                try:
                    async with self.http_slots:
                        bundle, receipt = await asyncio.to_thread(run_renewal, bundle)
                except AuthError as error:
                    raise ServiceError(error.code, 409) from None
                self.store.replace(owner, session_id, bundle, version)
                version += 1
            return bundle, version, deadline, receipt

    def status(self, owner, session_id):
        bundle, version, deadline = self.store.session(owner, session_id)
        cpf = bundle['cpf']
        return {'session_id': session_id, 'status': 'active' if remaining(bundle)>0 else 'renewal_needed',
                'cpf_mask': cpf[:3]+'.***.***-'+cpf[-2:], 'version': version,
                'retained_until': deadline,
                'token_validity': token_validity(bundle['headers']['Authorization'].removeprefix('Bearer '),
                                                  bundle['headers']['miToken']),
                'benefits': session_benefits(bundle['headers']['miToken']),
                'medical_reports': list_medical_reports(bundle['headers']['miToken'])}

    async def renewal(self, owner, session_id):
        _, _, _, receipt = await self.fresh(owner, session_id, force=True)
        return dict(self.status(owner, session_id), renewal=receipt)

    async def request(self, owner, session_id, path, query):
        # Validate before a potential renewal, so a rejected path has no upstream side effects.
        existing, _, _ = self.store.session(owner, session_id)
        resolve(path, query, existing)
        bundle, version, _, _ = await self.fresh(owner, session_id)
        try:
            async with self.http_slots:
                result = await asyncio.to_thread(fetch, bundle, path, query)
            if result['upstream_status'] in (401, 403):
                bundle, _, _, _ = await self.fresh(owner, session_id, force=True, expected=version)
                async with self.http_slots:
                    result = await asyncio.to_thread(fetch, bundle, path, query)
                if result['upstream_status'] in (401,403):
                    raise ServiceError('upstream_authentication_rejected', 409)
            return result
        except requests.RequestException:
            raise ServiceError('upstream_network_error', 502) from None

    async def export(self, owner, session_id):
        if not self.config.clients[owner].get('export_credentials', False):
            raise ServiceError('credentials_export_forbidden', 403)
        bundle, _, _, _ = await self.fresh(owner, session_id)
        # Export API credentials only. SSO cookies and passwords never leave the service.
        return {'base_url': API, 'headers': bundle['headers'],
                'token_validity': self.status(owner, session_id)['token_validity']}

    def close(self, owner, session_id):
        self.store.close(owner, session_id)
        self.session_locks.pop((owner, session_id), None)
        return {'session_id': session_id, 'status': 'closed'}


class AccessControl:
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope, receive, send)
        if scope['path']=='/healthz':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        value = headers.get(b'authorization', b'').decode('latin-1')
        token = value[7:] if value.startswith('Bearer ') else ''
        owner = None
        for name, entry in self.config.clients.items():
            if hmac.compare_digest(token.encode('utf-8'), entry['token'].encode('utf-8')):
                owner = name
        if owner is None:
            return await JSONResponse({'error': 'unauthorized'}, 401,
                headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})(scope, receive, send)
        marker = OWNER.set(owner)
        try:
            # Bound and buffer inputs before invoking either REST or the MCP SDK.
            body = bytearray()
            if scope['method'] in ('POST','PUT','PATCH'):
                while True:
                    message = await receive()
                    if message['type']=='http.disconnect':
                        return
                    body.extend(message.get('body', b''))
                    if len(body)>MAX_BODY:
                        return await JSONResponse({'error': 'request_too_large'}, 413)(scope, receive, send)
                    if not message.get('more_body', False):
                        break
            consumed = False
            async def replay():
                nonlocal consumed
                if not consumed and scope['method'] in ('POST','PUT','PATCH'):
                    consumed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            async def private_send(message):
                if message['type']=='http.response.start':
                    message['headers'] = list(message.get('headers', []))+[(b'cache-control',b'no-store'),
                        (b'x-content-type-options',b'nosniff')]
                await send(message)
            await self.app(scope, replay, private_send)
        finally:
            OWNER.reset(marker)


def create_app(config, *, service=None, start_worker=True):
    service = service or Service(config)
    mcp = MCPServer('meu-inss-remoto', version='1.0.0',
        instructions='Comece por inss_catalog. Crie o login e acompanhe o job; use session_id nas consultas. '
        'O servidor preenche CPF, valida NBs e guarda tokens. Não existe ferramenta MCP para exportar credenciais.')

    async def tool_call(action):
        try:
            result = action()
            return await result if hasattr(result, '__await__') else result
        except ServiceError as error:
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps({'error': error.code}))],
                                        isError=True)
        except Exception:
            return types.CallToolResult(content=[types.TextContent(type='text', text='{"error":"service_error"}')], isError=True)

    @mcp.tool()
    async def inss_catalog() -> dict:
        """List allowed read-only INSS paths, parameters and response formats."""
        return {'endpoints': CATALOG}

    @mcp.tool()
    async def inss_auth_start(cpf: str, password: str, idempotency_key: str | None = None):
        """Queue one authorized login. Credentials are encrypted while queued, then discarded; never retried automatically."""
        return await tool_call(lambda: service.enqueue(OWNER.get(), cpf, password, idempotency_key))

    @mcp.tool()
    async def inss_auth_status(job_id: str):
        """Poll a login job. On success use the returned session_id."""
        return await tool_call(lambda: service.store.job(OWNER.get(), job_id))

    @mcp.tool()
    async def inss_session_status(session_id: str):
        """Read token validity and session benefits/eligible medical reports without exposing credentials."""
        return await tool_call(lambda: service.status(OWNER.get(), session_id))

    @mcp.tool()
    async def inss_session_renew(session_id: str):
        """Renew with retained SSO cookies over HTTP. May require a fresh login; no password/browser retry."""
        return await tool_call(lambda: service.renewal(OWNER.get(), session_id))

    @mcp.tool()
    async def inss_session_close(session_id: str):
        """Delete the server-side session. Previously exported credentials may remain valid at INSS."""
        return await tool_call(lambda: service.close(OWNER.get(), session_id))

    @mcp.tool()
    async def inss_request(session_id: str, path: str, query: dict | None = None):
        """GET a catalog path. Keep {cpf} literal, substitute dates/NB. JSON is returned without tokens; PDFs as embedded resources."""
        async def action():
            result = await service.request(OWNER.get(), session_id, path, {} if query is None else query)
            body = result.pop('body', None)
            if body is not None:
                return types.CallToolResult(content=[types.EmbeddedResource(resource=types.BlobResourceContents(
                    uri='inss://document/'+session_id+'/'+path.rsplit('/',1)[-1], mimeType='application/pdf',
                    blob=base64.b64encode(body).decode()))],
                    structuredContent={'upstream_status':200, 'size_bytes':len(body)})
            return result
        return await tool_call(action)

    host = urlsplit(config.public_url).hostname
    transport = TransportSecuritySettings(allowed_hosts=[host, '127.0.0.1:*', 'localhost:*'],
                                         allowed_origins=[config.public_url])
    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True,
        max_request_body_size=MAX_BODY, transport_security=transport)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            worker = asyncio.create_task(service.work()) if start_worker else None
            app.state.worker = worker
            try:
                yield
            finally:
                if worker:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass

    async def body(request, keys):
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            raise ServiceError('invalid_json') from None
        if not isinstance(value, dict) or set(value)-set(keys):
            raise ServiceError('invalid_fields')
        return value

    async def endpoint(request, name):
        try:
            owner, params = OWNER.get(), request.path_params
            if name=='catalog':
                result = {'endpoints': CATALOG}
            elif name=='start':
                values = await body(request, ('cpf','password'))
                result = service.enqueue(owner, values.get('cpf'), values.get('password'), request.headers.get('Idempotency-Key'))
                return JSONResponse(result, 202)
            elif name=='job':
                result = service.store.job(owner, params['job_id'])
            elif name=='status':
                if request.method=='DELETE':
                    result = service.close(owner, params['session_id'])
                else:
                    result = service.status(owner, params['session_id'])
            elif name=='renew':
                result = await service.renewal(owner, params['session_id'])
            elif name=='export':
                result = await service.export(owner, params['session_id'])
            else:
                values = await body(request, ('path','query'))
                result = await service.request(owner, params['session_id'], values.get('path'), values.get('query', {}))
                pdf = result.pop('body', None)
                if pdf is not None:
                    return Response(pdf, media_type='application/pdf', headers={'Content-Disposition':'attachment; filename="inss-document.pdf"'})
                status = result['upstream_status']
                return JSONResponse(result, 200 if status==200 else 404 if status==404 else 502)
            return JSONResponse(result)
        except ServiceError as error:
            return JSONResponse({'error': error.code}, error.status)
        except Exception:
            return JSONResponse({'error': 'service_error'}, 500)

    async def health(request):
        if start_worker and app.state.worker.done():
            return JSONResponse({'status':'worker_unavailable'}, 503)
        return JSONResponse({'status':'ok', 'service':'meu-inss', 'version':'1.0.0'})

    def routed(name):
        async def handle(request):
            return await endpoint(request, name)
        return handle

    # Mount last at / to keep the SDK's exact /mcp route and its lifespan intact.
    app = Starlette(lifespan=lifespan, routes=[Route('/healthz', health),
        Route('/v1/catalog', routed('catalog')),
        Route('/v1/auth/jobs', routed('start'), methods=['POST']),
        Route('/v1/auth/jobs/{job_id}', routed('job')),
        Route('/v1/sessions/{session_id}', routed('status'), methods=['GET','DELETE']),
        Route('/v1/sessions/{session_id}/renew', routed('renew'), methods=['POST']),
        Route('/v1/sessions/{session_id}/credentials', routed('export'), methods=['POST']),
        Route('/v1/sessions/{session_id}/request', routed('request'), methods=['POST']),
        Mount('/', app=mcp_app)])
    app.state.service = service
    return AccessControl(app, config)


def main():
    import uvicorn
    os.umask(0o077)
    # SDK/HTTP debug logging can contain arguments or transport identifiers.
    logging.getLogger('mcp').setLevel(logging.CRITICAL)
    config = Config.from_file()
    uvicorn.run(create_app(config), host='0.0.0.0', port=8091, access_log=False, log_level='warning')


if __name__=='__main__':
    main()
