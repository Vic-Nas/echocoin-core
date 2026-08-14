# Echocoin build targets
#
# make linux   -- regenerate icons then build dist/echocoin        (Linux only)
# make windows -- build dist/echocoin.exe using pre-committed icons (Windows/Linux)
# make icons   -- regenerate favicon.ico and echocoin.png from echocoin.svg (Linux only,
#                 requires libcairo2-dev + pip install cairosvg Pillow)
#                 Commit the results so Windows builds don't need cairo.
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

.PHONY: linux windows appimage icons clean test

linux: icons
	pyinstaller --clean --noconfirm $(SPEC)
	@echo "Built: $(DIST)/echocoin"

windows:
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

appimage: linux
	@which appimagetool > /dev/null 2>&1 || (echo "appimagetool not found. Download from https://github.com/AppImage/AppImageKit/releases" && exit 1)
	rm -rf AppDir
	mkdir -p AppDir/usr/bin
	cp -r dist/echocoin AppDir/usr/bin/echocoin
	cp echocoin.png AppDir/echocoin.png
	printf '[Desktop Entry]\nName=Echocoin\nExec=echocoin\nIcon=echocoin\nType=Application\nCategories=Network;Finance;\n' > AppDir/echocoin.desktop
	printf '#!/bin/sh\nexec "$APPDIR/usr/bin/echocoin" "$$@"\n' > AppDir/AppRun
	chmod +x AppDir/AppRun
	ARCH=x86_64 appimagetool AppDir echocoin-x86_64.AppImage
	@echo "Built: echocoin-x86_64.AppImage"

test:
	python3 -m pytest tests/ -q

clean:
	rm -rf $(DIST) $(BUILD) __pycache__ echocoin_chain.db echocoin_chain.db-shm echocoin_chain.db-wal
	find . -name "*.pyc" -delete
