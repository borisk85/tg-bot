"""
Запусти один раз локально для получения refresh_token.
Не требует credentials.json — вводишь client_id и client_secret вручную.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/drive",
]

client_id = input("Client ID: ").strip()
client_secret = input("Client Secret: ").strip()

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

print("\n=== СКОПИРУЙ ЭТИ ЗНАЧЕНИЯ В Railway Variables ===")
print(f"GOOGLE_CLIENT_ID={creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
