import torch
import cv2
import numpy as np
from craft import CRAFT
from collections import OrderedDict
from craft_utils import getDetBoxes
from skimage import img_as_float32

def load_craft_model():
    model = CRAFT()
    model.load_state_dict(copyStateDict(torch.load("models/craft_mlt_25k.pth", map_location='cpu')))
    model.eval()
    return model

def copyStateDict(state_dict):
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = k.replace("module.", "")
        new_state_dict[name] = v
    return new_state_dict

def detect_text(model, image):
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = img_as_float32(image)
    x = torch.from_numpy(image).permute(2, 0, 1).unsqueeze(0)
    with torch.no_grad():
        y, _ = model(x)
    boxes, _ = getDetBoxes(y[0,:,:,0].numpy(), y[0,:,:,1].numpy(), 
                            text_threshold=0.7, link_threshold=0.4, low_text=0.4)
    return boxes
