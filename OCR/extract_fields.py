import re

def extract_fields(ocr_text):
    name = re.search(r'Name[:\-]?\s*([A-Za-z ]+)', ocr_text)
    dob = re.search(r'(DOB|D\.O\.B\.|Birth Date)[:\-]?\s*([\d/]+)', ocr_text)
    gender = re.search(r'(Male|Female|M|F)', ocr_text, re.IGNORECASE)
    aadhar = re.search(r'(\d{4}\s\d{4}\s\d{4})', ocr_text)
    father = re.search(r"(Father'?s Name|S/O|C/O)[:\-]?\s*([A-Za-z ]+)", ocr_text, re.IGNORECASE)
    address = re.search(r'Address[:\-]?\s*(.+)', ocr_text, re.IGNORECASE)

    fields = {
        "Name": name.group(1).strip() if name else None,
        "DOB": dob.group(2).strip() if dob else None,
        "Gender": gender.group(1).capitalize() if gender else None,
        "Aadhar_Number": aadhar.group(1) if aadhar else None,
        "Father_Name": father.group(2).strip() if father else None,
        "Address": address.group(1).strip() if address else None
    }
    return fields
