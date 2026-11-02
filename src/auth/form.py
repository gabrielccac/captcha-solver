"""Replay genérico de formulários HTML.

A ideia: não hardcodar nome de campo. Lê o `<form>`, copia todos os inputs
(inclusive hidden e submit) e só sobrescreve o que você manda. Assim o fluxo
sobrevive quando o portal renomeia um campo — que é o que mais quebra scraping
de SSO.
"""
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Form:
    action: str = ""
    method: str = "get"
    fields: dict[str, str] = field(default_factory=dict, repr=False)
    submit_name: str | None = None
    submit_value: str | None = None
    form_id: str | None = None

    def payload(self, **overrides) -> dict[str, str]:
        data = dict(self.fields)
        if self.submit_name:
            data[self.submit_name] = self.submit_value or ""
        data.update({k: v for k, v in overrides.items() if v is not None})
        return data


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._current: Form | None = None
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._current = Form(
                action=a.get("action", ""),
                method=(a.get("method") or "get").lower(),
                form_id=a.get("id") or None,
            )
            self.forms.append(self._current)
            self._in_form = True
            return
        if not self._in_form or self._current is None:
            return
        if "disabled" in a:
            return
        if tag == "button":
            if a.get("type", "submit").lower() == "submit" and a.get("name") and not self._current.submit_name:
                self._current.submit_name = a["name"]
                self._current.submit_value = a.get("value", "")
            return
        if tag == "input":
            name, typ = a.get("name"), (a.get("type") or "text").lower()
            if not name:
                return
            if typ in ("submit", "button", "reset", "image"):
                if typ == "submit" and not self._current.submit_name:
                    self._current.submit_name = name
                    self._current.submit_value = a.get("value", "")
                return
            if typ in ("checkbox", "radio") and "checked" not in a:
                return
            if typ == "password":
                # não copiamos valor; o chamador injeta
                self._current.fields.setdefault(name, "")
                return
            self._current.fields[name] = a.get("value", "on" if typ in ("checkbox", "radio") else "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
            self._current = None


def parse_forms(html: str) -> list[Form]:
    p = _Parser()
    p.feed(html)
    return p.forms


def find_form(html: str, *, form_id: str | None = None, has_field: str | None = None) -> Form | None:
    for f in parse_forms(html):
        if form_id and f.form_id != form_id:
            continue
        if has_field and has_field not in f.fields:
            continue
        return f
    return None
