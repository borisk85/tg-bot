"""
Запусти один раз локально для получения refresh_token.
После этого скрипт больше не нужен.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://mail.google.com/",  # полный доступ к Gmail (включая удаление)
]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

print("\n=== СКОПИРУЙ ЭТИ ЗНАЧЕНИЯ В .env ===")
print(f"GOOGLE_CLIENT_ID={creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
