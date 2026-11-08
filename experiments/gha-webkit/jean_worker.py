"""BPC partial-payment preview, using the same authenticated WebKit runtime."""
from calendar import monthrange
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import io
import json
import sys
from zoneinfo import ZoneInfo

from query_support import get_json, login_with_selection_retry, session_benefits, smoke


def date_value(value):
    if isinstance(value, str):
        for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(value[:10], fmt).date()
            except ValueError:
                continue
    return None


def preview(benefits, credits, today):
    candidates = [b for b in benefits
                  if str(b.get('benefit_type_code')) in ('87', '88')
                  and str(b.get('benefit_status', '')).upper() in ('ATIVO', 'ATIVA')]
    if len(candidates) != 1:
        return {'estado': 'conferir_nb', 'quantidade_nbs_bpc_ativos': len(candidates)}
    nb = candidates[0]['benefit_number']
    rows = [r for r in credits if str(r.get('nb')) == nb]
    if not rows:
        return {'estado': 'sem_previsao', 'nb_final': nb[-4:]}
    latest = rows[-1]
    day = date_value(latest.get('dtInicioValidade'))
    if day is None or day < today:
        return {'estado': 'sem_previsao_futura', 'nb_final': nb[-4:]}
    if latest.get('estorno') or latest.get('creditoInvalido') or latest.get('inCreditoPago'):
        return {'estado': 'conferir_credito', 'nb_final': nb[-4:]}
    try:
        value = Decimal(str(latest.get('valorLiquido')))
        if not value.is_finite() or value <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return {'estado': 'conferir_valor', 'nb_final': nb[-4:]}
    return {'estado': 'previsao_encontrada', 'nb_final': nb[-4:],
            'proposta': {'PRÓX PAGAMENTO': day.strftime('%d/%m/%Y'),
                         'VALOR PRÓX PAGAMENTO': float(value)}}


def main():
    data = {}
    original = smoke.http_login.login

    def login(session, cpf, password, api_key, **kwargs):
        receipt = original(session, cpf, password, api_key, **kwargs)
        access = session.headers['Authorization'].removeprefix('Bearer ')
        mi = session.headers['miToken']
        secret = (access, mi, password, api_key)
        today = datetime.now(ZoneInfo('America/Belem')).date()
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        end = today.replace(day=monthrange(today.year, today.month)[1])
        info = get_json(session, f'/usuarioservices/info/{cpf}?refreshPedidos=false', secret)
        response = get_json(session, f'/hiscreServices/historicocreditos/{int(cpf)}/'
                            f'{start:%d-%m-%Y}/{end:%d-%m-%Y}', secret)
        credits = response.get('data', {})
        rows = credits.get('creditosTO') if isinstance(credits, dict) else None
        data['userinfo'] = {'http_status': info.get('http_status'), 'data': {}}
        data['credits'] = {'http_status': response.get('http_status')}
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            data['credits']['data'] = {}
            try:
                benefits = session_benefits(mi)
            except (TypeError, ValueError):
                data['bpc_parcial'] = {'estado': 'conferir_beneficios'}
            else:
                data['bpc_parcial'] = preview(benefits, rows, today)
        else:
            data['bpc_parcial'] = {'estado': 'conferir_resposta_creditos'}
        return receipt

    smoke.http_login.login = login
    try:
        result = login_with_selection_retry(smoke.main, sys.stdin.readline())
    finally:
        smoke.http_login.login = original
    return {'validation': result, 'client_data': data}


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            payload = main()
    except BaseException as error:
        payload = {'validation': {'success': False, 'code': 'jean_worker_failed',
                                  'error_type': type(error).__name__}, 'client_data': {}}
    print(json.dumps(payload), flush=True)
    raise SystemExit(0 if payload['validation']['success'] else 3)
