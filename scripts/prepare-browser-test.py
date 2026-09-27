"""Snapshot local data for isolated UI tests; stop the test server first."""
import sqlite3
from pathlib import Path
from PIL import Image, ImageDraw

Path('.runtime').mkdir(exist_ok=True)
source=sqlite3.connect('db.sqlite3')
target=sqlite3.connect('.runtime/commerce-browser.sqlite3')
source.backup(target)
source.close()
target.close()
image=Image.new('RGBA',(240,80),'#2563eb')
ImageDraw.Draw(image).text((18,28),'ALTAB STUDIO',fill='white',font_size=24)
image.save('.runtime/test-logo.png')
print('Isolated browser test database and image fixture ready.')
