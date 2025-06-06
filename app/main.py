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
    
# === Aadhaar Data Reading Function ===
def adhaar_read_data(text):
    res = text.split()
    name = None
    dob = None
    adh = None
    sex = None
    text1 = []

    lines = text.split('\n')
    for lin in lines:
        s = lin.strip().replace('\n','').rstrip().lstrip()
        text1.append(s)

    if 'female' in text.lower():
        sex = "Female"
    elif 'male' in text.lower():
        sex = "Male"
    else:
        sex = "Other"
    
    text1 = list(filter(None, text1))
    text0 = text1[:]

    try:
        # Improved Name Extraction
        # Latin script names
        name_match = re.search(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)
        name = name_match.group(0) if name_match else None

        # DOB cleaning
        dob = text0[1][-10:].strip()
        dob = dob.replace('l', '/').replace('L', '/').replace('I', '/').replace('i', '/').replace('|', '/')
        dob = dob.replace('"', '/1').replace(":", "").replace(" ", "")

        # Aadhaar Number
        aadhaar_matches = re.findall(r"\b(?:\d{4} \d{4} \d{4}| X{4,8}\d{4})\b", text)
        adh = aadhaar_matches[0] if aadhaar_matches else None
        
    except Exception as e:
        logging.warning(f"Parsing exception: {e}")

    logging.info(f"📤 Parsed Aadhaar Data: {name=}, {dob=}, {adh=}, {sex=}")
    return {
        'Name': name,
        'Date of Birth': dob,
        'Aadhaar Number': adh,
        'Gender': sex,
        'ID Type': 'Aadhaar'
    }


# === Extract Aadhaar Info ===
def extract_info(text):
    data = adhaar_read_data(text)
    
    # Rename keys to match your schema
    info = {
        "Name": data.get("Name"),
        "Gender": data.get("Gender"),
        "Aadhaar Number": data.get("Aadhaar Number"),
        "VID": None,
        "Address": None,
        "Pincode": None,
    }

    # === Extract VID ===
    vid_matches = re.findall(r"(?:VID[:;]?\s*)(\d{4} \d{4} \d{4} \d{4})", text)
    if not vid_matches:
        vid_matches = re.findall(r"\b\d{4} \d{4} \d{4} \d{4}\b", text)
    if vid_matches:
        info["VID"] = vid_matches[0]

    # === Extract Pincode ===
    pincode_match = re.search(r'\b\d{6}\b', text)
    if pincode_match:
        info['Pincode'] = pincode_match.group(0)

    # === Extract Address Block ===
    # Look for C/O or ~/O line and grab next 1–3 lines until pincode
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    address_lines = []
    found = False
    for i, line in enumerate(lines):
        if re.search(r'(?:C/O|~/O)[:\s]', line, re.IGNORECASE):
            address_lines.append(line)
            found = True
            # Get up to 2 more lines below this
            for j in range(i + 1, min(i + 4, len(lines))):
                address_lines.append(lines[j])
            break

    if found:
        address_text = " ".join(address_lines)
        # Remove known tags
        address_text = re.sub(r'(?:C/O|~/O)[:\s]*', '', address_text, flags=re.IGNORECASE)
        info["Address"] = address_text.strip()

    if not info["Aadhaar Number"]:
        return JSONResponse(
            status_code=422,
            content={"error": "Essential fields missing", "text": text}
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
            text = pytesseract.image_to_string(rotated_img, lang='eng+hintel')
        except pytesseract.TesseractError:
            text = pytesseract.image_to_string(rotated_img, lang='eng+hin+tel')
        matches = re.findall(r'\b\d{4}\s\d{4}\s\d{4}\b', text)
        score = len(matches)
        if score > best_score:
            best_score = score
            best_text = text
            logging.info(f"🔄 Best OCR with rotation {angle}°, found {score} Aadhaar numbers")
        custom_oem_psm_config = r'--oem 3 --psm 6'
        text = pytesseract.image_to_string(rotated_img, lang='eng+hin+tel', config=custom_oem_psm_config)
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

    front_text = ocr_best_orientation(front_img)
    back_text = ocr_best_orientation(back_img)
    full_text = front_text + "\n" + back_text

    logging.info("🔍 Extracted text from front image:\n" + front_text)
    logging.info("🔍 Extracted text from back image:\n" + back_text)

    info = extract_info(full_text)

    if isinstance(info, JSONResponse):
        return info

    info['User ID'] = request.user_id

    if not info.get("Aadhaar Number") or not info.get("Name"):
        return JSONResponse(status_code=422, content={"error": "Essential fields missing", "text": full_text})

    saved = save_data(info)
    return {"status": "exists" if not saved else "saved", "data": info}