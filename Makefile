# Echocoin build targets
#
# make linux   -- build dist/echocoin        (run on Linux)
# make windows -- build dist/echocoin.exe    (run on Windows)
# make icons   -- regenerate favicon.ico and echocoin.png from echocoin.svg
# make clean   -- remove build artifacts
#
# PyInstaller must build on the target platform. Cross-compilation is not supported.
# VDF_ITERATIONS in params.py must be calibrated on the target hardware before
# building for mainnet (see vdf.py docstring).
#
# Requirements: pip install pyinstaller cairosvg Pillow

SPEC  = echocoin.spec
DIST  = dist
BUILD = build

.PHONY: linux windows icons clean test

linux: icons
	pyinstaller --clean --noconfirm $(SPEC)
	@echo "Built: $(DIST)/echocoin"

windows: icons
	pyinstaller --clean --noconfirm $(SPEC)
	@echo "Built: $(DIST)/echocoin.exe"

icons:
	python3 -c "\
import cairosvg, base64, io; \
from PIL import Image; \
imgs=[Image.open(io.BytesIO(cairosvg.svg2png(url='echocoin.svg',output_width=s,output_height=s))).convert('RGBA') for s in [16,32,48]]; \
imgs[0].save('favicon.ico',format='ICO',sizes=[(16,16),(32,32),(48,48)],append_images=imgs[1:]); \
open('echocoin.png','wb').write(cairosvg.svg2png(url='echocoin.svg',output_width=512,output_height=512)); \
print('icons regenerated')"

test:
	python3 -m pytest tests/ -q

clean:
	rm -rf $(DIST) $(BUILD) __pycache__ echocoin_chain.db echocoin_chain.db-shm echocoin_chain.db-wal
	find . -name "*.pyc" -delete
