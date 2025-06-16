import os
os.environ["CUDA_VISIBLE_DEVICES"] = ""

# main.py
import io
import re
import csv
import pickle
import logging
import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
import requests
import redis
from dotenv import load_dotenv
from docling_core.types.io import DocumentStream  # Add this import at the top

from docling.document_converter import DocumentConverter  # OCR & layout
load_dotenv()

# === Setup ===
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# === Redis Client ===
try:
    r = redis.Redis(
        host="localhost",
        port="6379",
        username="default",
        password="hqpl@123",
        decode_responses=True
    )
    r.ping()
    logging.info("✅ Redis connected")
except Exception as e:
    logging.error("❌ Redis unavailable")
    r = None

csv_path = "./aadhaar_data.csv"
pkl_path = "./aadhaar_data.pkl"
converter = DocumentConverter()  # load docling model :contentReference[oaicite:1]{index=1}

class AadhaarRequest(BaseModel):
    user_id: str
    front_url: str
    back_url: str

def download_image(url: str) -> Image.Image:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content))

def upscale_image(pil_img: Image.Image, hint: str) -> Image.Image:
    inp = f"/tmp/{hint}_in.png"
    out_dir = "/tmp"
    pil_img.save(inp)
    try:
        subprocess.run([
            "upscayl", "--input", inp, "--output", out_dir,
            "--scale", "2", "--mode", "real-esrgan"
        ], check=True)
        # find upscaled
        for f in os.listdir(out_dir):
            if f.startswith(hint) and f.endswith(".png"):
                return Image.open(os.path.join(out_dir, f))
    except Exception as e:
        logging.warning(f"Upscayl failed: {e}")
    return pil_img

def extract_text_from_image(img: Image.Image) -> str:
    # Resize image to max 1200px on the longest side before OCR
    max_side = 1200
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)
    # save PIL as bytes for docling: it accepts path or stream
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    doc_stream = DocumentStream(name="input.png", stream=buf)
    result = converter.convert(doc_stream)
    return result.document.export_to_markdown()

def extract_info(front_text: str, back_text: str = None):
    # Aadhaar Number (12 digits, with or without spaces) - search both front and back
    aadhaar_match = re.search(r"\b\d{4} ?\d{4} ?\d{4}\b", front_text)
    if not aadhaar_match and back_text:
        aadhaar_match = re.search(r"\b\d{4} ?\d{4} ?\d{4}\b", back_text)
    aadhaar_number = aadhaar_match.group(0).replace(" ", "") if aadhaar_match else None

    # Clean lines
    lines = [line.strip() for line in front_text.split("\n") if line.strip()]

    # Name: Try to extract from line containing gender, or from back_text if not found
    name = None
    skip_words = ["government of india", "republic of india", "unique identification", "authority", "aadhaar", "card", "male", "female", "dob", "year of birth", "address", "vid", "father", "mother", "image", "govt", "govt. of india"]
    # 1. Look for name in line with gender
    for i, line in enumerate(lines):
        if re.search(r"\bmale\b|पुरुष|\bfemale\b|महिला", line, re.IGNORECASE):
            # Remove gender word and try to extract name
            possible = re.sub(r"\b(male|female|पुरुष|महिला)\b", "", line, flags=re.IGNORECASE).strip()
            if possible and not any(w in possible.lower() for w in skip_words):
                name = possible
                break
    # 2. If not found, look for first valid line after gender line
    if not name:
        for i, line in enumerate(lines):
            if re.search(r"\bmale\b|पुरुष|\bfemale\b|महिला", line, re.IGNORECASE):
                if i+1 < len(lines):
                    possible = lines[i+1]
                    if not any(w in possible.lower() for w in skip_words) and len(possible.split()) >= 2:
                        name = possible
                        break
    # 3. If still not found, try back_text
    if not name and back_text:
        back_lines = [line.strip() for line in back_text.split("\n") if line.strip()]
        for i, line in enumerate(back_lines):
            if re.search(r"\bmale\b|पुरुष|\bfemale\b|महिला", line, re.IGNORECASE):
                possible = re.sub(r"\b(male|female|पुरुष|महिला)\b", "", line, flags=re.IGNORECASE).strip()
                if possible and not any(w in possible.lower() for w in skip_words):
                    name = possible
                    break
        if not name:
            for i, line in enumerate(back_lines):
                if re.search(r"\bmale\b|पुरुष|\bfemale\b|महिला", line, re.IGNORECASE):
                    if i+1 < len(back_lines):
                        possible = back_lines[i+1]
                        if not any(w in possible.lower() for w in skip_words) and len(possible.split()) >= 2:
                            name = possible
                            break
    # 4. Fallback: first valid line in front_text
    if not name:
        for line in lines:
            lcline = line.lower()
            if any(w in lcline for w in skip_words):
                continue
            if len(line.split()) >= 2 and re.match(r"^[A-Za-z .'-]+$", line):
                name = line.strip()
                break

    # Gender: look for gender words near DOB or Aadhaar number
    gender = None
    for i, line in enumerate(lines):
        if re.search(r"\bmale\b|पुरुष", line, re.IGNORECASE):
            gender = "Male"
            break
        elif re.search(r"\bfemale\b|महिला", line, re.IGNORECASE):
            gender = "Female"
            break

    # === Extract VID ===
    vid_matches = re.findall(r"(?:VID[:;]?\s*)(\d{4} \d{4} \d{4} \d{4})", front_text)
    if not vid_matches:
        vid_matches = re.findall(r"\b\d{4} \d{4} \d{4} \d{4}\b", front_text)
    if back_text and not vid_matches:
        vid_matches = re.findall(r"(?:VID[:;]?\s*)(\d{4} \d{4} \d{4} \d{4})", back_text)
        if not vid_matches:
            vid_matches = re.findall(r"\b\d{4} \d{4} \d{4} \d{4}\b", back_text)
    vid = vid_matches[0] if vid_matches else None

    # === Extract Pincode ===
    pincode_match = re.search(r'\b\d{6}\b', front_text)
    if not pincode_match and back_text:
        pincode_match = re.search(r'\b\d{6}\b', back_text)
    pincode = pincode_match.group(0) if pincode_match else None

    address = None
    if back_text:
        back_lines = [line.strip() for line in back_text.split("\n") if line.strip()]
        for i, line in enumerate(back_lines):
            if re.search(r'(?:C/O|~/O|S/O|W/O|D/O|H/O)[:\s]', line, re.IGNORECASE):
                address_lines = [line]
                for j in range(i + 1, min(i + 6, len(back_lines))):
                    address_lines.append(back_lines[j])
                    if re.search(r'\b\d{6}\b', back_lines[j]):
                        break
                address_text = " ".join(address_lines)
                address_text = re.sub(r'(?:C/O|~/O|S/O|W/O|D/O|H/O)[:\s]*', '', address_text, flags=re.IGNORECASE)
                address = address_text.strip()
                break
        if not address:
            addr_start, addr_end = -1, -1
            for i, line in enumerate(back_lines):
                if addr_start == -1 and re.search(r'address', line, re.IGNORECASE):
                    addr_start = i + 1
                if addr_start != -1 and re.search(r'\b\d{6}\b', line):
                    addr_end = i
                    break
            if addr_start != -1 and addr_end > addr_start:
                address = ' '.join(back_lines[addr_start:addr_end]).strip()
    if not address:
        for i, line in enumerate(lines):
            if re.search(r'(?:C/O|~/O|S/O|W/O|D/O|H/O)[:\s]', line, re.IGNORECASE):
                address_lines = [line]
                for j in range(i + 1, min(i + 5, len(lines))):
                    address_lines.append(lines[j])
                    if re.search(r'\b\d{6}\b', lines[j]):
                        break
                address_text = " ".join(address_lines)
                address_text = re.sub(r'(?:C/O|~/O|S/O|W/O|D/O|H/O)[:\s]*', '', address_text, flags=re.IGNORECASE)
                address = address_text.strip()
                break
        if not address:
            addr_start, addr_end = -1, -1
            for i, line in enumerate(lines):
                if addr_start == -1 and re.search(r'address', line, re.IGNORECASE):
                    addr_start = i + 1
                if addr_start != -1 and re.search(r'\b\d{6}\b', line):
                    addr_end = i
                    break
            if addr_start != -1 and addr_end > addr_start:
                address = ' '.join(lines[addr_start:addr_end]).strip()
    # Removed 422 error for missing Aadhaar number
    return {
        "Name": name,
        "Gender": gender,
        "Aadhaar Number": aadhaar_number,
        "VID": vid,
        "Address": address,
        "Pincode": pincode,
    }

def save_data(info: dict) -> bool:
    # same logic as yours
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

@app.post("/upload_url")
async def upload_via_url(req: AadhaarRequest):
    try:
        front, back = download_image(req.front_url), download_image(req.back_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image download invalid: {e}")

    front, back = upscale_image(front, "front"), upscale_image(back, "back")
    front_txt, back_txt = extract_text_from_image(front), extract_text_from_image(back)
    full_txt = front_txt + "\n" + back_txt
    logging.info("OCR text:\n" + full_txt)

    info = extract_info(front_txt, back_txt)
    if isinstance(info, JSONResponse):
        return info

    info.update({"User ID": req.user_id})
    # Remove all essential field checks, always return info
    saved = save_data(info)
    return {"status": "exists" if not saved else "saved", "data": info}

@app.get("/")
async def root():
    return {"message": "Welcome – Aadhar Extractor️"}

@app.get("/health")
async def health():
    return {
        "redis": bool(r and r.ping()),
        "csv": os.path.exists(csv_path),
        "pkl": os.path.exists(pkl_path),
    }
