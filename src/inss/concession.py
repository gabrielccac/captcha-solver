"""Extract labelled fields from text-based INSS concession letters."""
import re
import subprocess
import unicodedata
from datetime import datetime
from decimal import Decimal


def parse_concession_pdf(path):
    result = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
                            capture_output=True, text=True, timeout=15, check=True)
    return parse_concession_text(result.stdout)


def parse_concession_text(text):
    plain = "".join(c for c in unicodedata.normalize("NFD", text)
                    if unicodedata.category(c) != "Mn")
    flat = " ".join(plain.split())

    def match(pattern):
        found = re.search(pattern, flat, re.IGNORECASE)
        return found.group(1).strip() if found else None

    def date_after(label):
        value = match(label + r"\s*(\d{2}/\d{2}/\d{4})")
        try:
            return datetime.strptime(value, "%d/%m/%Y").date().isoformat() if value else None
        except ValueError:
            return None

    identity = re.search(r"NOME\s+NIT\s+OL\s+NB\s*\n([^\n]+)", text)
    columns = re.split(r"\s{2,}", identity.group(1).strip()) if identity else []
    declaration = re.search(r"CONCEDIDO\s+(.+?)\s*\((\d+)\)\s+([\d.-]+)\s+REQUERIDO", flat)
    amount = match(r"RENDA MENSAL INICIAL DE R\$\s*([\d.]+,\d{2})")
    bank = re.search(r"(?:ORG[AÃ]O) PAGADOR / AG[EÊ]NCIA BANC[AÁ]RIA:\s*\n([^\n]+)\n([^\n]+)", text)
    fields = {
        "document_date": date_after(r"DATA:"),
        "customer_name": columns[0] if len(columns) == 4 else None,
        "nit": re.sub(r"\D", "", columns[1]) if len(columns) == 4 else None,
        "local_office_code": columns[2] if len(columns) == 4 else None,
        "benefit_number": re.sub(r"\D", "", columns[3]) if len(columns) == 4 else None,
        "benefit_description": declaration.group(1) if declaration else None,
        "benefit_type_code": int(declaration.group(2)) if declaration else None,
        "request_date": date_after("REQUERIDO EM"),
        "initial_monthly_amount": str(Decimal(amount.replace(".", "").replace(",", "."))) if amount else None,
        "currency": "BRL" if amount else None,
        "payment_start_date": date_after("INICIO DO PAGAMENTO A PARTIR DE"),
        "bank_attendance_from": date_after("COMPARECA A PARTIR DE"),
        "monthly_payment_schedule": match(r"CREDITOS SUBSEQUENTES SERAO EFETUADOS NO\s+(.+?MES)\b"),
        "paying_bank_branch": bank.group(1).strip() if bank else None,
        "paying_bank_address": bank.group(2).strip() if bank else None,
        "first_payment_period_start": date_after("primeiro pagamento proporcional ao periodo de"),
        "first_payment_period_end": date_after(r"primeiro pagamento proporcional ao periodo de \d{2}/\d{2}/\d{4} a"),
        "credit_notices": [s.strip() for s in re.findall(r"\*{3}\s*(.*?)\s*\*{3}", flat)],
    }
    required = ("customer_name", "benefit_number", "benefit_type_code", "request_date",
                "initial_monthly_amount", "payment_start_date")
    fields["missing_fields"] = [key for key in required if fields[key] is None]
    fields["parse_status"] = "partial" if fields["missing_fields"] else "ok"
    # Other benefit types may contain calculation tables; keep their source text.
    fields["text"] = text.strip()
    return fields
