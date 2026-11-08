"""Only known read-only INSS paths; identifiers come from the owned session."""
from datetime import date, datetime
import re

from .hosted_store import ServiceError
from .medical import list_medical_reports
from .session import session_benefits

CATALOG = [
    {'path': '/usuarioservices/info/{cpf}', 'query': {'refreshPedidos': 'false'}, 'response': 'json',
     'description': 'Dados do usuário; CPF preenchido pela sessão; tokens removidos do retorno.'},
    {'path': '/hiscreServices/historicocreditos/{cpf}/{start}/{end}', 'query': {}, 'response': 'json',
     'description': 'Histórico de créditos. Substitua start/end por DD-MM-YYYY; CPF é preenchido pelo servidor.'},
    {'path': '/calculoBeneficioServices/concalPdf/{nb}', 'query': {}, 'response': 'pdf',
     'description': 'Carta de concessão para um NB listado na sessão.'},
    {'path': '/laudomedicoservices/pdf/{nb}', 'query': {}, 'response': 'pdf',
     'description': 'Laudo médico para um NB elegível listado na sessão; disponibilidade depende do INSS.'},
]


def resolve(path, query, bundle):
    if not isinstance(path, str) or len(path)>250 or not isinstance(query, dict):
        raise ServiceError('invalid_request')
    if path == CATALOG[0]['path']:
        if query not in ({}, {'refreshPedidos': False}, {'refreshPedidos': 'false'}):
            raise ServiceError('query_not_allowed')
        return path.replace('{cpf}', bundle['cpf']), {'refreshPedidos': 'false'}, 'json'
    match = re.fullmatch(r'/hiscreServices/historicocreditos/\{cpf\}/(\d{2}-\d{2}-\d{4})/(\d{2}-\d{2}-\d{4})', path)
    if match:
        try:
            start, end = (datetime.strptime(value, '%d-%m-%Y').date() for value in match.groups())
        except ValueError:
            raise ServiceError('invalid_dates') from None
        if start > end or end > date.today() or (end-start).days > 366:
            raise ServiceError('invalid_dates')
        if query:
            raise ServiceError('query_not_allowed')
        return path.replace('{cpf}', bundle['cpf'].lstrip('0') or '0'), {}, 'json'
    match = re.fullmatch(r'/(calculoBeneficioServices/concalPdf|laudomedicoservices/pdf)/([0-9]{1,15})', path)
    if match:
        if query:
            raise ServiceError('query_not_allowed')
        mi = bundle['headers']['miToken']
        allowed = list_medical_reports(mi) if match[1].startswith('laudomedico') else session_benefits(mi)
        if match[2] not in {benefit['benefit_number'] for benefit in allowed}:
            raise ServiceError('benefit_outside_session', 403)
        return path, {}, 'pdf'
    raise ServiceError('path_not_allowed', 403)


def redact_tokens(value, tokens=()):
    private = {'accesstoken', 'refreshtoken', 'idtoken', 'mitoken', 'token', 'jwt',
               'authorization', 'cookie', 'cookies', 'setcookie', 'password'}
    if isinstance(value, dict):
        return {key: redact_tokens(item, tokens) for key, item in value.items()
                if re.sub(r'[^a-z]', '', key.lower()) not in private}
    if isinstance(value, list):
        return [redact_tokens(item, tokens) for item in value]
    if isinstance(value, str):
        for token in tokens:
            if token:
                value = value.replace(token, '[redacted]')
    return value
