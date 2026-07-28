source .venv/bin/activate
python -c "
import requests, os
from dotenv import load_dotenv
load_dotenv()

token = os.getenv('TELEGRAM_BOT_TOKEN')
chat_id = os.getenv('TELEGRAM_CHAT_ID')

resp = requests.post(
    f'https://api.telegram.org/bot{token}/sendMessage',
    data={'chat_id': chat_id, 'text': '✅ Agentic T-Shirt System connected!'}
)
print(f'Status: {resp.status_code}')
print(resp.json())
"
