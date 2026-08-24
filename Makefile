# Scorchcoin build targets
#
# make linux   -- regenerate icons, build onedir via PyInstaller,
#                 then wrap into dist/scorchcoin.AppImage
# make windows -- build dist/scorchcoin.exe (onefile) using pre-committed icons
# make icons   -- regenerate favicon.ico and scorchcoin.png from scorchcoin.svg
#                 (Linux only; requires libcairo2-dev + pip install cairosvg Pillow)
#                 Run before make linux or make windows. Output is git-ignored.
# make clean   -- remove build artifacts
#
# PyInstaller must build on the target platform. Cross-compilation is not supported.
# VDF_ITERATIONS in params.py must be calibrated on the target hardware before
# building for mainnet (see vdf.py docstring).
# appimagetool must be on PATH for make linux:
#   wget -q https://github.com/AppImage/AppImageKit/releases/latest/download/appimagetool-x86_64.AppImage
#   chmod +x appimagetool-x86_64.AppImage && sudo mv appimagetool-x86_64.AppImage /usr/local/bin/appimagetool
#
# Requirements: pip install pyinstaller cairosvg Pillow

SPEC    = scorchcoin.spec
DIST    = dist
BUILD   = build
APPDIR  = AppDir

.PHONY: linux windows icons test clean

linux: icons
	pyinstaller --clean --noconfirm $(SPEC)
	@which appimagetool > /dev/null 2>&1 || (echo "appimagetool not found. See Makefile header." && exit 1)
	rm -rf $(APPDIR)
	mkdir -p $(APPDIR)/usr/bin
	cp -r $(DIST)/scorchcoin/* $(APPDIR)/usr/bin/
	cp scorchcoin.png $(APPDIR)/scorchcoin.png
	printf '[Desktop Entry]\nName=Scorchcoin\nExec=scorchcoin\nIcon=scorchcoin\nType=Application\nCategories=Network;Finance;\n' > $(APPDIR)/scorchcoin.desktop
	printf '#!/bin/sh\nexec "$$APPDIR/usr/bin/scorchcoin" "$$@"\n' > $(APPDIR)/AppRun
	chmod +x $(APPDIR)/AppRun
	ARCH=x86_64 appimagetool --appimage-extract-and-run $(APPDIR) $(DIST)/scorchcoin.AppImage
	@echo "Built: $(DIST)/scorchcoin.AppImage"

windows:
	pyinstaller --clean --noconfirm $(SPEC)
	@echo "Built: $(DIST)/scorchcoin.exe"

icons:
	python3 -c "\
import cairosvg, base64, io; \
from PIL import Image; \
imgs=[Image.open(io.BytesIO(cairosvg.svg2png(url='scorchcoin.svg',output_width=s,output_height=s))).convert('RGBA') for s in [16,32,48]]; \
imgs[0].save('favicon.ico',format='ICO',sizes=[(16,16),(32,32),(48,48)],append_images=imgs[1:]); \
open('scorchcoin.png','wb').write(cairosvg.svg2png(url='scorchcoin.svg',output_width=512,output_height=512)); \
print('icons regenerated')"

test:
	python3 -m pytest tests/ -q

clean:
	rm -rf $(DIST) $(BUILD) $(APPDIR) __pycache__ scorchcoin_chain.db scorchcoin_chain.db-shm scorchcoin_chain.db-wal
	find . -name "*.pyc" -delete
