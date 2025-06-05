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
from dotenv import load_dotenv

# === Setup ===
app = FastAPI()
logging.basicConfig(level=logging.INFO)
load_dotenv()

# === Paths ===
csv_path = "./aadhaar_data.csv"
pkl_path = "./aadhaar_data.pkl"

# === Redis Client ===
try:
    r = redis.Redis(
        host=os.getenv("REDIS_HOST"),
        port=int(os.getenv("REDIS_PORT")),
        password=os.getenv("REDIS_PASSWORD"),
        decode_responses=True
    )
    r.ping()
    logging.info("✅ Redis connected")
except redis.exceptions.ConnectionError as e:
    logging.error("❌ Redis connection failed")
    logging.error(e)
    r = None

# === Request Model ===
class AadhaarRequest(BaseModel):
    user_id: str
    front_url: str
    back_url: str

# === Extract Aadhaar Info ===
def extract_info(text):
    import re

    info = {}
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    text_lower = text.lower()

    # Aadhaar Number
    aadhaar = re.findall(r'\b\d{4}\s\d{4}\s\d{4}\b', text)
    info['Aadhaar Number'] = aadhaar[0] if aadhaar else None

    # VID
    vid = re.findall(r'\b\d{4}\s\d{4}\s\d{4}\s\d{4}\b', text)
    info['VID'] = vid[0] if vid else None

    # DOB
    dob_match = re.search(r'(\d{2}[/-]\d{2}[/-]\d{4})', text)
    info['DOB'] = dob_match.group(1) if dob_match else None

    # Gender
    if re.search(r'\bmale\b|sx[: ]*m', text_lower):
        info['Gender'] = 'Male'
    elif re.search(r'\bfemale\b|sx[: ]*f', text_lower):
        info['Gender'] = 'Female'
    else:
        info['Gender'] = None

    # Name (avoid common header lines)
    ignore_lines = ['government of india', 'भारत सरकार', 'unique identification']
    name = None
    for i, line in enumerate(lines):
        line_clean = line.strip().lower()
        if any(x in line_clean for x in ignore_lines):
            continue
        if re.match(r"^[A-Z][a-zA-Z '.]{2,}$", line.strip()) and len(line.strip().split()) <= 5:
            name = line.strip()
            break
    info['Name'] = name

    # Address
    address_keywords = ['S/O', 'C/O', 'H.No', 'House', 'Dist:', 'Village']
    address_lines = []
    for i, line in enumerate(lines):
        if any(k.lower() in line.lower() for k in address_keywords):
            address_lines.append(line.strip())
            for j in range(1, 3):  # Try to add next 2 lines
                if i + j < len(lines):
                    address_lines.append(lines[i + j].strip())
            break
    info['Address'] = ' '.join(address_lines) if address_lines else None

    # Pincode
    pin = re.search(r'\b\d{6}\b', text)
    info['Pincode'] = pin.group(0) if pin else None
    
    essential_fields = ['Aadhaar Number', 'VID', 'DOB', 'Gender', 'Name', 'Address', 'Pincode']
    missing = [field for field in essential_fields if not info.get(field)]

    if missing:
        return JSONResponse(
            status_code=422,
            content={"error": "Essential fields missing", "missing_fields": missing, "text": text}
        )

    return info

# === Save Data ===
def save_data(info):
    try:
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
            filtered_info = {k: v for k, v in info.items() if v is not None}
            r.hset(redis_key, mapping=filtered_info)
            logging.info(f"✅ Data saved to Redis under key: {redis_key}")
        return True
    except Exception as e:
        logging.error("❌ Error while saving data:")
        logging.error(e)
        return False

# === Download Image from URL ===
def download_image(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content))
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Image download failed: {e}")

# === OCR with Best Orientation ===
def ocr_best_orientation(image, lang='eng+hin+tel'):
    best_text = ""
    best_score = 0
    for angle in [0, 90, 180, 270]:
        rotated_img = image.rotate(angle, expand=True)
        try:
            text = pytesseract.image_to_string(rotated_img, lang=lang)
        except pytesseract.TesseractError:
            text = pytesseract.image_to_string(rotated_img, lang='eng')
        matches = re.findall(r'\b\d{4}\s\d{4}\s\d{4}\b', text)
        score = len(matches)
        if score > best_score:
            best_score = score
            best_text = text
            logging.info(f"🔄 Best OCR with rotation {angle}°, found {score} Aadhaar numbers")
        custom_oem_psm_config = r'--oem 3 --psm 6'
        text = pytesseract.image_to_string(rotated_img, lang=lang, config=custom_oem_psm_config)
    return best_text

# === Routes ===
@app.get("/")
async def root():
    return {"message": "Welcome to the Aadhaar Info Extractor API"}

@app.get("/health")
async def health_check():
    redis_ok = False
    if r:
        try:
            redis_ok = r.ping()
        except:
            redis_ok = False
    return {
        "redis": redis_ok,
        "csv_exists": os.path.exists(csv_path),
        "pkl_exists": os.path.exists(pkl_path)
    }

@app.post("/upload_url")
async def upload_via_url(request: AadhaarRequest):
    front_img = download_image(request.front_url)
    back_img = download_image(request.back_url)

    front_text = ocr_best_orientation(front_img, lang='eng+hin+tel')
    back_text = ocr_best_orientation(back_img, lang='eng+hin+tel')
    full_text = front_text + "\n" + back_text

    logging.info("🔍 Extracted text from front image:\n" + front_text)
    logging.info("🔍 Extracted text from back image:\n" + back_text)

    info = extract_info(full_text)

    # If extract_info returned an error response, return early
    if isinstance(info, JSONResponse):
        return info

    # Now it's safe to add user_id
    info['User ID'] = request.user_id

    if not info.get("Aadhaar Number") or not info.get("Name"):
        return JSONResponse(status_code=422, content={"error": "Essential fields missing", "text": full_text})

    saved = save_data(info)
    return {"status": "exists" if not saved else "saved", "data": info}
