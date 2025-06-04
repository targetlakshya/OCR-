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
    info = {
        "Name": None,
        "DOB": None,
        "Gender": None,
        "Aadhaar Number": None,
        "VID": None,
        "Address": None,
    }

    # Normalize text
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    text_all = " ".join(lines)

    # Aadhaar Number (4-4-4 digit format)
    aadhaar_match = re.search(r"\b\d{4} \d{4} \d{4}\b", text_all)
    if aadhaar_match:
        info["Aadhaar Number"] = aadhaar_match.group()

    # VID (16-digit number)
    # VID: match 16-digit number near "VID" keyword
    vid_matches = re.findall(r"(?:VID[:;]?\s*)(\d{4} \d{4} \d{4} \d{4})", text_all)
    if not vid_matches:
        # fallback to just 16-digit numbers
        vid_matches = re.findall(r"\b\d{4} \d{4} \d{4} \d{4}\b", text_all)

    if vid_matches:
        info["VID"] = vid_matches[0]


    # DOB / Year of Birth
    dob_match = re.search(r"\b(?:DOB|D0B|DoB|Birth|Year of Birth|ജനന തിയ്യതി|పుట్టిన తేదీ|பிறந்த தேதி)[^\d]*(\d{2}[/-]\d{2}[/-]\d{4}|\d{4})", text_all, re.IGNORECASE)
    if dob_match:
        info["DOB"] = dob_match.group(1)

    # Gender (Hindi, Telugu, English)
    if re.search(r"\b(Male|पुरुष|పురుషుడు)\b", text_all, re.IGNORECASE):
        info["Gender"] = "Male"
    elif re.search(r"\b(Female|महिला|స్త్రీ)\b", text_all, re.IGNORECASE):
        info["Gender"] = "Female"
    elif re.search(r"\b(Other|अन्य|ఇతరులు)\b", text_all, re.IGNORECASE):
        info["Gender"] = "Other"

    # Name — assume it comes after "Name" keyword or appears before DOB line
    for i, line in enumerate(lines):
        if info["Name"]:
            break
        if re.search(r"\b(Name|నామం|नाम)\b", line, re.IGNORECASE):
            # Get next line if "Name" is a keyword header
            if i + 1 < len(lines):
                info["Name"] = lines[i + 1]
        elif info["DOB"] and i > 0 and re.search(info["DOB"], line):
            # Previous line to DOB might be name
            info["Name"] = lines[i - 1]

    # Address detection — using 'Address' keyword and PIN code pattern
    def extract_address(lines):
        address_keywords = [
            "address", "s/o", "c/o", "w/o", "h.no", "house", "door", "village", 
            "post", "dist", "mandalam", "mandal", "nagar", "pincode", "near", "road", "colony"
        ]
        
        address_block = []
        in_address = False
        keyword_found = False
        for i, line in enumerate(lines):
            lower_line = line.lower()
            if any(keyword in lower_line for keyword in address_keywords):
                in_address = True
                keyword_found = True

            if in_address:
                address_block.append(line.strip())
                # Stop if we see a pincode (6 digit)
                if re.search(r"\b\d{6}\b", line):
                    break
                # Or if we go 4 lines ahead (assuming full address is captured)
                if len(address_block) >= 4:
                    break

        if address_block and keyword_found:
            address_text = " ".join(address_block)
            address_text = re.sub(r"\s{2,}", " ", address_text)
            address_text = re.sub(r"[^\w\s,./:-]", "", address_text)
            return address_text
        return None
        
    # Pincode
    pin = re.search(r'\b\d{6}\b', text)
    info['Pincode'] = pin.group(0) if pin else None

    # Validation
    if not info["Aadhaar Number"] or not info["Name"]:
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

    # 👇 Check if extract_info returned JSONResponse
    if isinstance(info, JSONResponse):
        return info

    # ✅ Now it's safe to assign
    info['User ID'] = request.user_id

    if not info.get("Aadhaar Number") or not info.get("Name"):
        return JSONResponse(status_code=422, content={"error": "Essential fields missing", "text": full_text})

    saved = save_data(info)
    return {"status": "exists" if not saved else "saved", "data": info}
