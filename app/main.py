from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
import pytesseract
import re
import os
import pickle
import csv
import requests
import redis
import logging
import io

# === Setup ===
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# === Paths ===
csv_path = "./aadhaar_data.csv"
pkl_path = "./aadhaar_data.pkl"

# === Redis Client ===
try:
    r = redis.Redis(
        host="localhost",
        port=6379,
        password="hqpl@123",  # 🔑 Your Redis password
        decode_responses=True
    )
    r.ping()
    print("✅ Redis connected")
except redis.exceptions.ConnectionError as e:
    print("❌ Redis connection failed")
    print(e)
    r = None

# === Request Model ===
class AadhaarRequest(BaseModel):
    user_id: str
    front_url: str
    back_url: str


# === Aadhaar Info Extractor ===
def extract_info(text):
    import re
    info = {}

    # Aadhaar number extraction
    aadhaar_matches = re.findall(r'\b\d{4}\s\d{4}\s\d{4}\b', text)
    info['Aadhaar Number'] = aadhaar_matches[0] if aadhaar_matches else None

    # DOB extraction
    dob_match = re.search(r'(?i)(DOB|D.O.B|जन्म तिथि)[^\d]*(\d{2}/\d{2}/\d{4})', text)
    info['DOB'] = dob_match.group(2) if dob_match else None

    # Gender extraction
    if re.search(r'(?i)\bmale\b|पुरुष', text):
        info['Gender'] = 'Male'
    elif re.search(r'(?i)\bfemale\b|महिला', text):
        info['Gender'] = 'Female'
    else:
        info['Gender'] = None

    # Extract Name: line with capitalized words (simple heuristic)
    lines = text.split('\n')
    name = None
    for line in lines:
        line = line.strip()
        if re.match(r'^[A-Z][a-zA-Z]*([ ][A-Z][a-zA-Z]*)+', line):
            name = line
            break
    info['Name'] = name

    # Address extraction with regex capturing until Aadhaar number or VID or end of text
    address = None
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if re.search(r'(?i)(Address|पता)', line):
            addr_lines = []
            # Collect next 3-5 lines as address
            for j in range(i + 1, min(i + 6, len(lines))):
                if re.search(r'\b\d{4}\s\d{4}\s\d{4}\b|VID', lines[j]):  # Stop if Aadhaar or VID comes
                    break
                if lines[j].strip():  # Skip empty lines
                    addr_lines.append(lines[j].strip())
            address = ' '.join(addr_lines).strip()
            break
    info['Address'] = address

    return info

# === Save Data ===
def save_data(info):
    if os.path.exists(pkl_path):
        with open(pkl_path, 'rb') as f:
            all_data = pickle.load(f)
    else:
        all_data = []

    existing_aadhaars = [entry['Aadhaar Number'] for entry in all_data if 'Aadhaar Number' in entry]
    if info['Aadhaar Number'] in existing_aadhaars:
        logging.info(f"⚠️ Aadhaar {info['Aadhaar Number']} already exists.")
        return False

    all_data.append(info)
    with open(pkl_path, 'wb') as f:
        pickle.dump(all_data, f)

    file_exists = os.path.exists(csv_path)
    with open(csv_path, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=info.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(info)

    if r:
        redis_key = f"aadhaar:{info['Aadhaar Number']}"
        # Filter out None values before saving to Redis
        filtered_info = {k: v for k, v in info.items() if v is not None}
        r.hset(redis_key, mapping=filtered_info)
        logging.info(f"✅ Data saved to Redis under key: {redis_key}")
    return True


# === Helper to Download and Read Image ===
def download_image(url):
    response = requests.get(url)
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="Image URL not accessible")
    return Image.open(io.BytesIO(response.content))


# === Endpoint ===
@app.get("/")
async def root():
    return {"message": "Welcome to the Aadhaar Info Extractor API"}


@app.post("/upload_url")
async def upload_via_url(request: AadhaarRequest):
    try:
        front_img = download_image(request.front_url)
        back_img = download_image(request.back_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image download failed: {str(e)}")

    front_text = pytesseract.image_to_string(front_img, lang='eng+hin')
    back_text = pytesseract.image_to_string(back_img, lang='eng+hin')
    full_text = front_text + "\n" + back_text
    
    

    info = extract_info(full_text)
    info['User ID'] = request.user_id

    if not info.get("Aadhaar Number") or not info.get("Name"):
        return JSONResponse(status_code=422, content={"error": "Essential fields missing", "text": full_text})

    saved = save_data(info)
    return {"status": "exists" if not saved else "saved", "data": info}

