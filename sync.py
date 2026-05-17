import os
import json
import base64
import re
import time
from html.parser import HTMLParser
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google import genai

# --- Configuracion ---
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
OUTPUT_FILE = "transacciones.json"
DAYS_BACK = 30
BATCH_SIZE = 5

BANK_SENDERS = [
    "alertasynotificaciones@an.notificacionesbancolombia.com",
    "notificaciones@clienteitau.co",
]

CATEGORY_OPTIONS = [
    "food", "transport", "market", "health",
    "ocio", "ropa", "servicios", "salary", "freelance", "otro"
]


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.skip_tags = {"script", "style", "head"}
        self.current_skip = None

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self.current_skip = tag

    def handle_endtag(self, tag):
        if tag == self.current_skip:
            self.current_skip = None

    def handle_data(self, data):
        if self.current_skip is None:
            text = data.strip()
            if text:
                self.text_parts.append(text)

    def get_text(self):
        return " ".join(self.text_parts)


def html_to_text(html):
    parser = HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_bank_emails(service):
    since = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y/%m/%d")
    query = f"after:{since} ({' OR '.join(f'from:{s}' for s in BANK_SENDERS)})"
    print(f"\nBuscando: {query}\n")
    result = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = result.get("messages", [])
    print(f"Encontrados: {len(messages)} correos\n")
    return messages


def extract_email_text(service, msg_id):
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

    sender = headers.get("From", "")
    date_str = headers.get("Date", "")
    subject = headers.get("Subject", "")
    plain_body = ""
    html_body = ""

    def extract_parts(parts_list):
        nonlocal plain_body, html_body
        for part in parts_list:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            if mime == "text/plain" and not plain_body and data:
                plain_body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            elif mime == "text/html" and not html_body and data:
                html_body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            elif "multipart" in mime:
                extract_parts(part.get("parts", []))

    extract_parts(payload.get("parts", [payload]))

    if not plain_body and not html_body:
        data = payload.get("body", {}).get("data", "")
        if data:
            raw = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            if "<html" in raw.lower():
                html_body = raw
            else:
                plain_body = raw

    body = plain_body if plain_body else html_to_text(html_body) if html_body else ""
    body = re.sub(r"\s+", " ", body).strip()
    return sender, date_str, subject, body[:2000]


def extract_batch(client, emails):
    items = ""
    for idx, (sender, date_str, subject, body) in enumerate(emails):
        items += f"""
--- CORREO {idx} ---
Remitente: {sender}
Asunto: {subject}
Fecha: {date_str}
Cuerpo: {body}
"""

    prompt = f"""Eres un extractor de transacciones bancarias colombianas.
Analiza cada correo y extrae los datos de transaccion.

Categorias validas: {json.dumps(CATEGORY_OPTIONS)}

{items}

Responde SOLO con un array JSON valido, un objeto por correo, en el mismo orden.
Sin texto adicional, sin backticks, sin explicaciones.

Para cada correo:
- Si NO es notificacion de transaccion (publicidad, bienvenida, etc): {{"skip": true}}
- Si SI es transaccion:
{{
  "type": "expense" o "income",
  "amount": numero entero en pesos sin puntos ni comas,
  "note": "comercio o descripcion max 50 chars",
  "cat": una de las categorias validas,
  "date": "ISO 8601 ej: 2025-05-10T14:30:00"
}}

Reglas cat: restaurantes/cafes/domicilios=food, bus/taxi/uber/gasolina=transport,
supermercado/drogueria=market, salud/farmacia=health, streaming/cine/bar=ocio,
ropa/zapatos=ropa, agua/luz/internet/telefono=servicios, nomina/sueldo=salary,
freelance/clientes=freelance, resto=otro"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw = response.text.strip()
    raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Falta la API key. Corre:")
        print("  export GEMINI_API_KEY=AIza...")
        return

    client = genai.Client(api_key=api_key)

    print("Conectando a Gmail...")
    service = get_gmail_service()

    messages = fetch_bank_emails(service)
    if not messages:
        print(f"No hay correos de bancos en los ultimos {DAYS_BACK} dias.")
        return

    existing = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        print(f"Ya hay {len(existing)} transacciones guardadas. Se agregan las nuevas.\n")

    existing_ids = {t.get("gmail_id") for t in existing if t.get("gmail_id")}

    print("Descargando correos...")
    pending = []
    for msg in messages:
        msg_id = msg["id"]
        if msg_id in existing_ids:
            continue
        sender, date_str, subject, body = extract_email_text(service, msg_id)
        if body:
            pending.append((msg_id, sender, date_str, subject, body))
            print(f"  {subject[:60]}")

    print(f"\n{len(pending)} correos nuevos para procesar en batches de {BATCH_SIZE}.\n")

    nuevas = []
    errors = 0
    skipped = 0

    for batch_start in range(0, len(pending), BATCH_SIZE):
        batch = pending[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(pending) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Procesando batch {batch_num}/{total_batches} ({len(batch)} correos)...", flush=True)

        try:
            email_data = [(s, d, sub, b) for _, s, d, sub, b in batch]
            results = extract_batch(client, email_data)

            for idx, result in enumerate(results):
                msg_id = batch[idx][0]
                subject = batch[idx][3]
                if result.get("skip"):
                    print(f"  - {subject[:50]} -> no es transaccion")
                    skipped += 1
                else:
                    result["source"] = "email"
                    result["gmail_id"] = msg_id
                    nuevas.append(result)
                    print(f"  + {result['type']} ${result['amount']:,} | {result['note']}")

        except Exception as e:
            print(f"  Error en batch: {type(e).__name__}: {e}")
            errors += 1

        if batch_start + BATCH_SIZE < len(pending):
            time.sleep(3)

    todas = existing + nuevas
    with open(OUTPUT_FILE, "w") as f:
        json.dump(todas, f, ensure_ascii=False, indent=2)

    print(f"\nListo.")
    print(f"  Nuevas transacciones: {len(nuevas)}")
    print(f"  No eran transacciones: {skipped}")
    print(f"  Errores de batch: {errors}")
    print(f"  Total en {OUTPUT_FILE}: {len(todas)}")


if __name__ == "__main__":
    main()
