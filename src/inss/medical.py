"""List session benefits eligible for medical reports and download a chosen PDF."""
import re
from pathlib import Path

import requests

from inss.session import API, api_session, session_benefits


def list_medical_reports(mi):
    # Same benefit types offered by Meu INSS's laudos-medicos page.
    return [dict(benefit, availability="not_checked") for benefit in session_benefits(mi)
            if benefit["benefit_type_code"] in (31, 32, 36, 87, 88, 91, 92, 94, 95, 96)]


def download_medical_report(auth, mi, benefit_number, output_dir):
    nb = str(benefit_number)
    if not re.fullmatch(r"[0-9]{1,15}", nb):
        raise ValueError("Invalid medical report benefit number")
    if nb not in {report["benefit_number"] for report in list_medical_reports(mi)}:
        raise ValueError("Medical report benefit outside current session")
    result = {"benefit_number": nb, "http_status": None, "file": None}
    try:
        client, headers = api_session(auth, mi)
        with client:
            response = client.get(f"{API}/laudomedicoservices/pdf/{nb}",
                                  headers=headers, timeout=30, allow_redirects=False)
        result["http_status"] = response.status_code
        if response.status_code != 200:
            result["error"] = ("Medical report unavailable" if response.status_code == 404
                               else "Medical report request failed")
        elif not response.content.startswith(b"%PDF-"):
            result["error"] = "Response is not a PDF"
        else:
            path = Path(output_dir) / f"laudo-medico-{nb}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as fh:
                path.chmod(0o600)
                fh.write(response.content)
            result.update(file=path.name, size_bytes=len(response.content))
    except requests.RequestException:
        result["error"] = "Medical report request timed out or could not connect"
    return result
