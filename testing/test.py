import os
import requests
from PIL import Image
from io import BytesIO
from docling.document_converter import DocumentConverter
import torch

# Check PyTorch CUDA availability
if torch.cuda.is_available():
    print("[WARNING] CUDA is available! Forcing CPU mode, but torch sees GPU. This may cause OOM errors.")
else:
    print("[INFO] Torch is running in CPU-only mode. No CUDA device will be used.")

# Create a folder to store downloaded images
if not os.path.exists("downloads"):
    os.makedirs("downloads")

# Image URLs
image_urls = {
    "front": "https://cdn.qoneqt.com/uploads/28600/aadhar_front_u05NO2LQ5I.jpg",
    "back": "https://cdn.qoneqt.com/uploads/28600/aadhar_back_Crn8JDbPJk.jpg"
}

# Download and save images
for name, url in image_urls.items():
    response = requests.get(url)
    if response.status_code == 200:
        image_path = f"downloads/{name}.jpg"
        with open(image_path, 'wb') as f:
            f.write(response.content)
        print(f"{name.capitalize()} image downloaded successfully.")
    else:
        print(f"Failed to download {name} image.")

# Initialize docling DocumentConverter
converter = DocumentConverter()

# Extract text from both images
for name in image_urls.keys():
    image_path = f"downloads/{name}.jpg"
    result = converter.convert(image_path)
    print(f"\nExtracted Text from {name.capitalize()} Image:")
    print(result.document.export_to_markdown())
