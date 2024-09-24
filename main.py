import base64
import os
import time
from email import message_from_bytes
from email.parser import BytesParser
import json
import functions_framework  # Required for Cloud Functions
from openai import OpenAI
from openai import OpenAIError, RateLimitError
from google.auth.transport.requests import Request
from google.cloud import storage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
import requests

# From Open AI
OPEN_AI_KEY=""
# From Telegram
TELEGRAM_BOT_TOKEN =""
YOUR_TELEGRAM_CHAT_ID=""
# From Google Cloud Platform
YOUR_BUCKET_NAME=""
TOKEN_JSON_PATH=""
CREDENTIALS_JSON_PATH=""

# Set your OpenAI API key securely using environment variables
client = OpenAI( api_key =OPEN_AI_KEY)

# Define the scopes and other constants
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
BUCKET_NAME = YOUR_BUCKET_NAME  # Set this as an environment variable
STATE_FILENAME = 'processed_email_ids.json'

def send_telegram_message(message_text):
    bot_token = TELEGRAM_BOT_TOKEN 
    chat_id = YOUR_TELEGRAM_CHAT_ID

    if not bot_token or not chat_id:
        print("Telegram bot token or chat ID not set.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message_text
    }

    try:
        response = requests.post(url, data=payload)
        if response.status_code != 200:
            print(f"Failed to send message via Telegram. Status code: {response.status_code}, Response: {response.text}")
        else:
            print("Telegram message sent successfully.")
    except Exception as e:
        print(f"An error occurred while sending Telegram message: {e}")


def authenticate_gmail():
    creds = None
    # Load credentials from token.json or generate it using credentials.json
    if os.path.exists(TOKEN_JSON_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_JSON_PATH, SCOPES)
    else:
        if os.path.exists(CREDENTIALS_JSON_PATH):
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_JSON_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_JSON_PATH, 'w') as token:
                token.write(creds.to_json())
        else:
            raise Exception("credentials.json file is missing.")

    # Refresh the credentials if they are expired
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        raise Exception("Credentials are invalid and cannot be refreshed.")
    return creds

def get_gmail_service():
    creds = authenticate_gmail()
    service = build('gmail', 'v1', credentials=creds)
    return service

def get_email_body(msg):
    payload = msg.get('payload', {})
    parts = payload.get('parts', [])
    body = ''

    def extract_parts(parts_list):
        nonlocal body
        for part in parts_list:
            if part.get('mimeType') == 'text/plain':
                data = part['body'].get('data')
                if data:
                    decoded_data = base64.urlsafe_b64decode(data).decode('utf-8')
                    body += decoded_data
            elif part.get('parts'):
                extract_parts(part.get('parts'))

    if parts:
        extract_parts(parts)
    else:
        data = payload['body'].get('data')
        if data:
            body = base64.urlsafe_b64decode(data).decode('utf-8')

    return body



def process_with_ai(email_content):
    MAX_EMAIL_LENGTH = min(len(email_content),2000)  # Adjust based on your needs
    email_content = email_content[:MAX_EMAIL_LENGTH]

    head_text = """
    Please analyze the following email and check if it falls into one of these categories:
    Is this an interview invitation for a job?
    Is it a rejection notice from a company?
    Is it a notification about a job opening related to data science or AI?
    Is it an acceptance for a job?
    
    Respond with the appropriate message, starting with the corresponding number:
    For an interview: "101 Great news! You have an interview scheduled on [date] at [time] with [company] for the position of [position]."
    For a rejection: "102 Unfortunately, it looks like [company] has decided to move forward without you for the [position] role."
    For a data science/AI-related job: "103 Heads up! There's an open position at [company] for the role of [role]."
    For an acceptance: "104 Congratulations! You've been accepted for the [position] role at [company]."
    If it doesn't match any of these: "105."
    Feel free to adjust the format if any details are missing, so it flows naturally.
    """
    prompt = head_text.strip() + "\nEmail Content:\n" + email_content.strip()

    messages = [{"role": "user", "content": prompt}]

    retry_delay = 5  # Start with a 5-second delay
    max_retries = 5
    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )
            ai_response = completion.choices[0].message.content.strip()
            return ai_response
        except RateLimitError as e:
            print(f"Rate limit exceeded: {e}")
            if attempt < max_retries - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                print("Max retries reached. Exiting.")
                return "Unable to process the request due to rate limits."
        except OpenAIError as e:
            print(f"An error occurred: {e}")
            break
    return "Unable to process the request at this time."

def read_processed_email_ids():
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(STATE_FILENAME)

    if blob.exists():
        data = blob.download_as_text()
        processed_ids = json.loads(data)
        return set(processed_ids)  # Use a set for efficient lookups
    else:
        return set()

def write_processed_email_ids(processed_ids):
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(STATE_FILENAME)
    data = json.dumps(list(processed_ids))
    blob.upload_from_string(data)

@functions_framework.cloud_event
def fetch_and_process_emails(cloud_event):
    gmail_service = get_gmail_service()

    # Load the set of processed email IDs from Cloud Storage
    processed_email_ids = read_processed_email_ids()

    # Retrieve emails
    results = gmail_service.users().messages().list(userId='me', labelIds=['INBOX'], maxResults=100).execute()
    messages = results.get('messages', [])

    if not messages:
        print('No messages found.')
        return

    # Process emails in reverse order (oldest first)
    messages.reverse()

    new_processed_ids = set()

    for message in messages:
        message_id = message['id']
        if message_id in processed_email_ids:
            # Skip emails that have already been processed
            continue

        msg = gmail_service.users().messages().get(userId='me', id=message_id, format='full').execute()
        email_body = get_email_body(msg)

        # Process the email content using the AI model
        ai_response = process_with_ai(email_body)
        if not "105" in ai_response:
            send_telegram_message(ai_response)
        print(f"AI Response: {ai_response}")

        # Add the email ID to the set of processed IDs
        new_processed_ids.add(message_id)

    # Update the set of processed email IDs
    processed_email_ids.update(new_processed_ids)
    write_processed_email_ids(processed_email_ids)