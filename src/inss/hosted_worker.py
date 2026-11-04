"""Isolated login worker. Its private stdout pipe is consumed only by the service."""
import http.cookiejar
import json
import sys

import requests

from auth.govbr import AuthError
from .http_login import login


def freeze(session, cpf):
    return {'cpf': cpf, 'headers': dict(session.headers),
            'cookies': [dict(cookie.__dict__) for cookie in session.cookies]}


def thaw(bundle):
    session = requests.Session()
    session.headers.clear()
    session.headers.update(bundle['headers'])
    for raw in bundle['cookies']:
        values = dict(raw)
        values['rest'] = values.pop('_rest', {})
        session.cookies.set_cookie(http.cookiejar.Cookie(**values))
    return session


def main():
    receipt = {}
    try:
        values = json.loads(sys.stdin.readline(16384))
        with requests.Session() as session:
            login(session, values['cpf'], values['password'], values['api_key'], receipt=receipt,
                  optimize_interactions=True, batch_captcha=True)
            bundle = freeze(session, values['cpf'])
            values.clear()
        result = {'success': True, 'bundle': bundle, 'timing': receipt}
    except AuthError as error:
        result = {'success': False, 'code': error.code}
    except Exception:
        result = {'success': False, 'code': 'login_worker_error'}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == '__main__':
    main()
