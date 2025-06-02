import requests
from PIL import Image
import io

url = 'https://www.dropbox.com/scl/fi/xplqdheanyyhqbp3spgyk/pawan-front.png?rlkey=oyv1rvivnzy6gkeqf412pdpis&st=ny2tb5zx&dl=0'

resp = requests.get(url)
print(resp.status_code)
print(resp.headers.get('content-type'))

img = Image.open(io.BytesIO(resp.content))  # This fails if content is not image bytes
img.show()
