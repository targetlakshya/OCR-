from preprocess import preprocess_image
from detect_text import load_craft_model, detect_text_regions
from recognize_text import recognize_text
from extract_fields import extract_fields
import cv2

# Process single image (front or back)
def process_image(image_path, craft_model):
    thresh, orig_image = preprocess_image(image_path)
    boxes = detect_text_regions(craft_model, orig_image)
    
    full_text = ""
    for box in boxes:
        x_min, y_min = box[:, 0].min(), box[:, 1].min()
        x_max, y_max = box[:, 0].max(), box[:, 1].max()
        cropped = orig_image[int(y_min):int(y_max), int(x_min):int(x_max)]
        text = recognize_text(cropped)
        full_text += text + "\n"
    return full_text

def run_aadhar_pipeline(front_image_path, back_image_path):
    craft_model = load_craft_model()

    print("Processing front image...")
    front_text = process_image(front_image_path, craft_model)
    print("Front OCR Result:\n", front_text)
    
    print("\nProcessing back image...")
    back_text = process_image(back_image_path, craft_model)
    print("Back OCR Result:\n", back_text)

    # Merge both texts
    merged_text = front_text + "\n" + back_text
    fields = extract_fields(merged_text)

    print("\nExtracted Fields:")
    for key, value in fields.items():
        print(f"{key}: {value}")

if __name__ == "__main__":
    run_aadhar_pipeline("/Users/hqpl/Desktop/Lakshya/OCR/OCR/OCR/images/front1.png", "/Users/hqpl/Desktop/Lakshya/OCR/OCR/OCR/images/back1.png")
