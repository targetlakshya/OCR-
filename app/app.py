from fastapi import FastAPI
from pydantic import BaseModel, HttpUrl
from PIL import Image
import requests
from io import BytesIO
import pytesseract
import os
import openai
import csv
from datetime import datetime
import json
import re
from dotenv import load_dotenv

load_dotenv()

client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()

CSV_FILE = "aadhaar_responses.csv"

class AadhaarURLRequest(BaseModel):
    user_id: str
    front_url: HttpUrl
    back_url: HttpUrl

def clean_response(text: str) -> str:
    # Remove Markdown-style code block wrappers
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    return cleaned.strip()


def save_to_csv(user_id: str, aadhaar_info: dict):
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['timestamp', 'User ID', 'Name', 'DOB', 'Gender', 'Aadhaar Number', 'VID', 'Address', 'Pincode']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        row = {
            'timestamp': datetime.utcnow().isoformat(),
            'User ID': user_id,
            'Name': aadhaar_info.get('Name', ''),
            'DOB': aadhaar_info.get('DOB', ''),
            'Gender': aadhaar_info.get('Gender', ''),
            'Aadhaar Number': aadhaar_info.get('Aadhaar Number', ''),
            'VID': aadhaar_info.get('VID', ''),
            'Address': aadhaar_info.get('Address', ''),
            'Pincode': aadhaar_info.get('Pincode', '')
        }
        writer.writerow(row)

def check_duplicate(aadhaar_number: str, vid: str) -> bool:
    if not os.path.isfile(CSV_FILE):
        return False
    with open(CSV_FILE, mode='r', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if (aadhaar_number and row.get('Aadhaar Number', '') == aadhaar_number) or \
               (vid and row.get('VID', '') == vid):
                return True
    return False

def find_all_aadhaar_vid(text):
    """
    Extract Aadhaar number and VID from text using regex.
    Returns tuple (aadhaar_number, vid)
    """
    # Aadhaar: 12 digits, usually grouped in 4-4-4 or continuous digits
    aadhaar_pattern = re.compile(r'\b\d{4}\s?\d{4}\s?\d{4}\b')
    # VID: 16 digits, possibly grouped in 4s with spaces
    vid_pattern = re.compile(r'\b\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b')

    aadhaar_match = aadhaar_pattern.search(text.replace('\n', ' '))
    vid_match = vid_pattern.search(text.replace('\n', ' '))

    aadhaar_number = aadhaar_match.group().replace(' ', '') if aadhaar_match else ""
    vid = vid_match.group().replace(' ', '') if vid_match else ""

    return aadhaar_number, vid

def valid_12_digit(s):
    return bool(re.fullmatch(r'\d{12}', s))

def valid_16_digit(s):
    return bool(re.fullmatch(r'\d{16}', s))

@app.post("/upload_url/")
async def upload_aadhaar_url(payload: AadhaarURLRequest):
    try:
        ocr_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz,.-/ '

        front_resp = requests.get(payload.front_url)
        back_resp = requests.get(payload.back_url)

        front_img = Image.open(BytesIO(front_resp.content))
        back_img = Image.open(BytesIO(back_resp.content))

        front_text = pytesseract.image_to_string(front_img, config=ocr_config)
        back_text = pytesseract.image_to_string(back_img, config=ocr_config)
        combined_text = front_text + "\n" + back_text

        if len(combined_text.strip()) < 20:
            return {"status": "error", "message": "OCR output is too short to extract information."}

        messages = [
            {
                "role": "system",
                "content": """
You are an assistant extracting Aadhaar card information from OCR text. 
Extract the following Aadhaar fields from this text:
- Name
- Date of Birth (DOB)
- Gender
- Aadhaar Number (exact 12 digits)
- VID Number (exact 16 digits)
- Address
- Pincode
- Aadhaar Number must be exactly 12 digits, no letters or spaces.
- VID must be exactly 16 digits.
- Return JSON only.
- If you cannot find the exact value, return empty string "" for that field.
"""
            },
            {
                "role": "system",
                "content": f"""
Extract the following Aadhaar fields from this text:
- Name
- Date of Birth (DOB)
- Gender
- Aadhaar Number (exact 12 digits)
- VID Number (exact 16 digits)
- Address
- Pincode

Text:
\"\"\"
{combined_text}
\"\"\"

Return result as a JSON object only:
{{"Name": "...", "DOB": "...", "Gender": "...", "Aadhaar Number": "...", "VID": "...", "Address": "...", "Pincode": "..."}}
                """
            }
        ]

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0,
            max_tokens=500,
        )

        answer_text = response.choices[0].message.content
        cleaned_text = clean_response(answer_text)
        print("🧹 Cleaned GPT JSON:", cleaned_text)


        try:
            aadhaar_info = json.loads(cleaned_text)
        except json.JSONDecodeError:
            return {
                "status": "error",
                "message": "Failed to parse JSON from GPT response.",
                "raw_response": answer_text
            }

        # Extract Aadhaar and VID from OCR text as fallback
        aadhaar_from_text, vid_from_text = find_all_aadhaar_vid(combined_text)

        # Validate and replace GPT output if invalid
        if not valid_12_digit(aadhaar_info.get('Aadhaar Number', '')):
            aadhaar_info['Aadhaar Number'] = aadhaar_from_text

        if not valid_16_digit(aadhaar_info.get('VID', '')):
            aadhaar_info['VID'] = vid_from_text

        if check_duplicate(aadhaar_info.get('Aadhaar Number', ''), aadhaar_info.get('VID', '')):
            return {
                "status": "exists",
                "message": "Aadhaar Number or VID already exists in the records."
            }

        save_to_csv(payload.user_id, aadhaar_info)

        return {
            "status": "saved",
            "data": {**aadhaar_info, "User ID": payload.user_id}
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}