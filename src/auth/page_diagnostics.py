"""Classify an intermediate login page without exporting text, values or URLs."""
from html.parser import HTMLParser
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit


class Signals(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.has_form = False
        self.has_button = False
        self.has_otp = False
        self.has_refresh = False
        self.text = []
        self.heading = []
        self.in_heading = False
        self.structure = []
        self.element_counts = {'forms': 0, 'inputs': 0, 'buttons': 0, 'iframes': 0, 'scripts': 0}

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.element_counts['scripts'] += 1
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag in ('form', 'input', 'button', 'iframe'):
            self.element_counts[{'form': 'forms', 'input': 'inputs', 'button': 'buttons', 'iframe': 'iframes'}[tag]] += 1
        self.structure.append(tag + ':' + str(attrs.get('type', '')) if tag == 'input' else tag)
        self.in_heading |= tag in ('h1', 'h2')
        self.has_form |= tag == 'form'
        self.has_button |= tag == 'button'
        if tag == 'input' and str(attrs.get('type', 'text')).lower() not in ('hidden', 'submit', 'button'):
            names = [str(attrs.get(k, '')).lower() for k in ('name', 'id')]
            self.has_otp |= (attrs.get('autocomplete') == 'one-time-code'
                             or any(name in ('otpinput', 'otp', 'totp', 'totpinput') for name in names))
        self.has_refresh |= tag == 'meta' and str(attrs.get('http-equiv', '')).lower() == 'refresh'

    def handle_endtag(self, tag):
        if tag in ('h1', 'h2'):
            self.in_heading = False
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.text.append(value)
            if self.in_heading:
                self.heading.append(value)


def classify_page(html, url, client_url, *, known_kind='unknown', status=200, hops=0):
    parser = Signals()
    parser.feed(html)
    text = ''.join(c for c in unicodedata.normalize('NFD', ' '.join(parser.text)).lower()
                   if not unicodedata.combining(c))
    heading = ''.join(c for c in unicodedata.normalize('NFD', ' '.join(parser.heading)).lower()
                      if not unicodedata.combining(c))
    public_error_signals = {
        'request_rejected': bool(re.search(
            r'\brequest rejected\b|\brequested url was rejected\b|\byour support id\b|'
            r'\bsolicitacao rejeitada\b|\burl solicitada foi rejeitada\b|\bid de suporte\b', text)),
        'access_denied_or_blocked': bool(re.search(
            r'\baccess denied\b|\bforbidden\b|\brequest blocked\b|\bacesso negado\b|'
            r'\brequisicao bloqueada\b|\bsolicitacao bloqueada\b|\bproibido\b', text)),
        'service_or_gateway_unavailable': bool(re.search(
            r'\bservice unavailable\b|\btemporarily unavailable\b|\bbad gateway\b|'
            r'\bservico (?:temporariamente )?indisponivel\b|\bservico fora do ar\b|\berro de gateway\b', text)),
        'cookies_required_or_disabled': bool(re.search(
            r'\benable cookies\b|\bcookies? (?:are )?disabled\b|\bcookies? must be enabled\b|'
            r'\bhabilite (?:os )?cookies\b|\bative (?:os )?cookies\b|'
            r'\bcookies? (?:estao )?(?:desativad[oa]s?|desabilitad[oa]s?)\b', text)),
        'session_or_request_invalid': bool(re.search(
            r'\b(?:session|request) (?:has been )?(?:expired|invalid)\b|\binvalid (?:session|request)\b|'
            r'\bsessao (?:expirada|encerrada|invalida)\b|\bsolicitacao invalida\b|\brequisicao invalida\b', text)),
    }
    signals = []
    if parser.has_otp or re.search(r'verificacao em duas etapas|autenticacao em dois fatores', heading):
        kind = 'two_factor'
        signals.append('otp_input' if parser.has_otp else 'two_factor_heading')
    elif re.search(r'\b(?:h-?)?captcha\s+(?:invalid[oa]|incorret[oa]|expirad[oa]|nao\s+(?:foi\s+)?(?:aceito|validado))\b|nao foi possivel validar o captcha', text):
        kind = 'captcha_error'
        signals.append('captcha_error_message')
    elif (parser.has_form or parser.has_button) and re.search(r'atualiz(?:e|ar).*(?:dados|senha)|(?:altere|crie|cadastre).*(?:nova )?senha|complete seu cadastro', heading):
        kind = 'account_update'
        signals.append('account_update_heading')
    elif known_kind in ('account', 'password'):
        kind = known_kind
        signals.append('login_form')
    elif (parser.has_form or parser.has_button) and re.search(r'compartilhamento de dados|autorizar (?:o )?acesso|permissao de acesso', text):
        kind = 'consent'
        signals.append('authorization_form')
    elif (parser.has_form or parser.has_button) and re.search(r'atualiz(?:e|ar) (?:os )?seus dados|complete seu cadastro', text):
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
    fingerprint = hashlib.sha256('|'.join(parser.structure).encode()).hexdigest()[:16]
    return dict(kind=kind, route=route, http_status=status, hops=hops, signals=signals,
                public_error_signals=public_error_signals, html_counts=parser.element_counts,
                structure_fingerprint=fingerprint,
                provider_error_codes=sorted(set(re.findall(r'\berl\d{7}\b', text)))[:8])


def is_upstream_rejection_page(diagnostic):
    return (diagnostic.get('kind') == 'unknown'
            and diagnostic.get('public_error_signals', {}).get('request_rejected') is True)
