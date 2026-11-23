"""Classify an intermediate login page without exporting text, values or URLs."""
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urlsplit


class Signals(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.has_form = False
        self.has_otp = False
        self.has_refresh = False
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        self.has_form |= tag == 'form'
        if tag == 'input' and str(attrs.get('type', 'text')).lower() not in ('hidden', 'submit', 'button'):
            names = [str(attrs.get(k, '')).lower() for k in ('name', 'id')]
            self.has_otp |= (attrs.get('autocomplete') == 'one-time-code'
                             or any(name in ('otpinput', 'otp', 'totp', 'totpinput') for name in names))
        self.has_refresh |= tag == 'meta' and str(attrs.get('http-equiv', '')).lower() == 'refresh'

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.text.append(value)


def classify_page(html, url, client_url, *, known_kind='unknown', status=200, hops=0):
    parser = Signals()
    parser.feed(html)
    text = ''.join(c for c in unicodedata.normalize('NFD', ' '.join(parser.text)).lower()
                   if not unicodedata.combining(c))
    signals = []
    if parser.has_otp:
        kind = 'two_factor'
        signals.append('otp_input')
    elif known_kind in ('account', 'password'):
        kind = known_kind
        signals.append('login_form')
    elif parser.has_form and re.search(r'compartilhamento de dados|autorizar (?:o )?acesso|permissao de acesso', text):
        kind = 'consent'
        signals.append('authorization_form')
    elif parser.has_form and re.search(r'atualiz(?:e|ar) (?:os )?seus dados|complete seu cadastro', text):
        kind = 'account_update'
        signals.append('account_update_form')
    elif re.search(r'sessao (?:expirada|encerrada)', text):
        kind = 'session_expired'
    elif re.search(r'servico (?:temporariamente )?indisponivel|erro interno|tente novamente mais tarde', text):
        kind = 'service_error'
    elif parser.has_refresh:
        kind = 'script_navigation'
        signals.append('meta_refresh')
    else:
        kind = 'unknown'
    parts = urlsplit(url)
    if parts.netloc == urlsplit(client_url).netloc:
        route = 'client_callback'
    elif parts.netloc != 'sso.acesso.gov.br':
        route = 'other_origin'
    elif '/authorize' in parts.path:
        route = 'govbr_authorize'
    elif '/login' in parts.path:
        route = 'govbr_login'
    elif re.search(r'otp|two.factor|mfa', parts.path, re.I):
        route = 'govbr_two_factor'
    else:
        route = 'govbr_other'
    return dict(kind=kind, route=route, http_status=status, hops=hops, signals=signals)
