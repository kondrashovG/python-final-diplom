# Import YAML module
from requests import get
from yaml import load as load_yaml, Loader
from orders.settings import MEDIA_ROOT

# Load YAML data from the file
# with open(f'{MEDIA_ROOT}data/shop1.yaml', encoding='UTF-8') as fh:
#     read_data = load_yaml(fh, Loader=Loader)

url = 'https://drive.google.com/file/d/1VAKDLFFqZs3YMiWuINH-pRDQfBf3Y8y2/view'
stream = get(url).content
print(stream)

read_data = load_yaml(stream, Loader=Loader)
# Print YAML data before sorting
print(read_data)
print(read_data['shop'])
