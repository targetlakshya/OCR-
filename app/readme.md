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
from dotenv import load_dotenv

# === Paths ===
csv_path = "./aadhaar_data.csv"
pkl_path = "./aadhaar_data.pkl"

load_dotenv()  # load variables from .env

# === Redis Client ===
try:
    r = redis.Redis(
    host=os.getenv("REDIS_HOST"),
    port=int(os.getenv("REDIS_PORT")),
    password=os.getenv("REDIS_PASSWORD"),
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
def extract_info(text, source='front'):
    info = {}

    if source == 'front':
        # Aadhaar Number
        # Aadhaar Number (12-digit)
        aadhaar_match = re.search(r'\b\d{4} \d{4} \d{4}\b', text)
        info['Aadhaar Number'] = aadhaar_match.group() if aadhaar_match else None

        # VID (16-digit)
        vid_match = re.search(r'VID\s*:?\s*(\d{4} \d{4} \d{4} \d{4})', text)
        info['VID'] = vid_match.group(1) if vid_match else None

        # DOB
        dob_match = re.search(r'(?i)(DOB|D.O.B|जन्म[\s]*तिथि)[^\d]*(\d{2}[-/]\d{2}[-/]\d{4})', text)
        if dob_match:
            info['DOB'] = dob_match.group(2)
        else:
            dob_fallback = re.search(r'\b\d{2}[-/]\d{2}[-/]\d{4}\b', text)
            info['DOB'] = dob_fallback.group() if dob_fallback else None

        # Gender
        if re.search(r'(?i)\bmale\b|पुरुष', text):
            info['Gender'] = 'Male'
        elif re.search(r'(?i)\bfemale\b|महिला', text):
            info['Gender'] = 'Female'
        else:
            info['Gender'] = None

        # Name
        lines = text.split('\n')
        name = None
        # Improved Name extraction: Look for capitalized words without digits, and not labels
        for line in lines:
            if re.match(r'^[A-Z][a-z]+(?: [A-Z][a-z]+)+$', line.strip()) and not any(x in line.lower() for x in ['male', 'female', 'dob', 'vid']):
                name = line.strip()
                break

        info['Name'] = name

    elif source == 'back':
        pin_match = re.search(r'\b\d{6}\b', text)
        lines = text.split('\n')
        address = None

        if pin_match:
            # Find the line with the pincode
            pin_line_idx = next((i for i, line in enumerate(lines) if re.search(r'\b\d{6}\b', line)), -1)
            if pin_line_idx != -1:
                addr_lines = []
                # Collect 3 lines above and 3 below
                for j in range(max(0, pin_line_idx - 3), min(len(lines), pin_line_idx + 4)):
                    l = lines[j].strip()
                    if l and not re.search(r'\bVID\b|\b\d{4} \d{4} \d{4}\b', l):
                        addr_lines.append(l)
                address = ' '.join(addr_lines).strip()

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



def ocr_best_orientation(image, lang='eng+hin',):
    """Try OCR on 0, 90, 180, 270 degrees rotation and return best text based on Aadhaar pattern match."""
    best_text = ""
    best_score = 0  # number of Aadhaar-like matches found
    for angle in [0, 90, 180, 270]:
        rotated_img = image.rotate(angle, expand=True)
        text = pytesseract.image_to_string(rotated_img, lang=lang)
        matches = re.findall(r'\b\d{4}\s\d{4}\s\d{4}\b', text)
        score = len(matches)
        if score > best_score:
            best_score = score
            best_text = text
            logging.info(f"🔄 Best OCR with rotation {angle}°, found {score} Aadhaar numbers")
    return best_text


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

    front_text = ocr_best_orientation(front_img, lang='eng+hin')
    back_text = ocr_best_orientation(back_img, lang='eng+hin')

    front_info = extract_info(front_text, source='front')
    back_info = extract_info(back_text, source='back')

    # Merge both dicts
    info = {**front_info, **back_info}
    info['User ID'] = request.user_id

    missing_fields = [k for k in ["Aadhaar Number", "Name", "DOB", "Gender", "Address"] if not info.get(k)]
    if missing_fields:
        return JSONResponse(status_code=422, content={
            "error": "Essential fields missing",
            "missing_fields": missing_fields,
            "text": front_text + "\n" + back_text
        })

    saved = save_data(info)
    return {"status": "exists" if not saved else "saved", "data": info}
