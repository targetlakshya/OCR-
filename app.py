import pytesseract
from PIL import Image
import re
import csv
import os
import pickle

# Paths
csv_path = "/Users/hqpl/Desktop/Lakshya/OCR/OCR/aadhaar_data.csv"
pkl_path = "/Users/hqpl/Desktop/Lakshya/OCR/OCR/aadhaar_data.pkl"

# Load Aadhaar images
front_img = Image.open("/Users/hqpl/Desktop/Lakshya/OCR/OCR/lakshya-front.jpeg")
back_img = Image.open("/Users/hqpl/Desktop/Lakshya/OCR/OCR/lakshya-back.jpeg")

# OCR using Tesseract (English + Hindi)
front_text = pytesseract.image_to_string(front_img, lang='eng+hin')
back_text = pytesseract.image_to_string(back_img, lang='eng+hin')

# Combine text
full_text = front_text + "\n" + back_text

# Function to extract Aadhaar fields
def extract_info(text):
    info = {}

    aadhaar_match = re.search(r'(\d{4}\s\d{4}\s\d{4})', text)
    if aadhaar_match:
        info['Aadhaar Number'] = aadhaar_match.group(1)

    name_match = re.search(r'(?i)(Name|नाम)\s*[:\-]?\s*([A-Za-z ]{3,})', text)
    if name_match:
        info['Name'] = name_match.group(2).strip()

    dob_match = re.search(r'(?i)(DOB|D.O.B|जन्म तिथि)\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})', text)
    if dob_match:
        info['DOB'] = dob_match.group(2)

    if re.search(r'(?i)Male|पुरुष', text):
        info['Gender'] = 'Male'
    elif re.search(r'(?i)Female|महिला', text):
        info['Gender'] = 'Female'

    address_match = re.search(r'(?i)(Address|पता)\s*[:\-]?\s*(.*?)(?=\d{4}\s\d{4}\s\d{4}|VID|$)', text, re.DOTALL)
    if address_match:
        address = ' '.join(address_match.group(2).split())
        info['Address'] = address

    return info

# Extract new data
new_data = extract_info(full_text)

# Load existing data from .pkl
if os.path.exists(pkl_path):
    with open(pkl_path, 'rb') as f:
        all_data = pickle.load(f)
else:
    all_data = []

# Check for duplicate Aadhaar number
aadhaar_numbers = [entry['Aadhaar Number'] for entry in all_data if 'Aadhaar Number' in entry]
new_aadhaar = new_data.get('Aadhaar Number')

if new_aadhaar in aadhaar_numbers:
    matched_entry = next((entry for entry in all_data if entry.get('Aadhaar Number') == new_aadhaar), None)
    print(f"⚠️ Aadhaar data already exists and the name {matched_entry['Name']} belongs to that Aadhaar user.")
else:
    all_data.append(new_data)
    with open(pkl_path, 'wb') as f:
        pickle.dump(all_data, f)

    file_exists = os.path.isfile(csv_path)
    with open(csv_path, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=new_data.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(new_data)

    print(f"✅ New Aadhaar data saved to CSV and PKL.")
