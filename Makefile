# Echocoin build targets
#
# make linux   -- regenerate icons, build onedir via PyInstaller,
#                 then wrap into dist/echocoin.AppImage
# make windows -- build dist/echocoin.exe (onefile) using pre-committed icons
# make icons   -- regenerate favicon.ico and echocoin.png from echocoin.svg
#                 (Linux only; requires libcairo2-dev + pip install cairosvg Pillow)
#                 Commit the results so Windows builds don't need cairo.
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

SPEC    = echocoin.spec
DIST    = dist
BUILD   = build
APPDIR  = AppDir

.PHONY: linux windows icons test clean

linux: icons
	pyinstaller --clean --noconfirm $(SPEC)
	@which appimagetool > /dev/null 2>&1 || (echo "appimagetool not found. See Makefile header." && exit 1)
	rm -rf $(APPDIR)
	mkdir -p $(APPDIR)/usr/bin
	cp -r $(DIST)/echocoin/* $(APPDIR)/usr/bin/
	cp echocoin.png $(APPDIR)/echocoin.png
	printf '[Desktop Entry]\nName=Echocoin\nExec=echocoin\nIcon=echocoin\nType=Application\nCategories=Network;Finance;\n' > $(APPDIR)/echocoin.desktop
	printf '#!/bin/sh\nexec "$$APPDIR/usr/bin/echocoin" "$$@"\n' > $(APPDIR)/AppRun
	chmod +x $(APPDIR)/AppRun
	ARCH=x86_64 appimagetool --appimage-extract-and-run $(APPDIR) $(DIST)/echocoin.AppImage
	@echo "Built: $(DIST)/echocoin.AppImage"

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

test:
	python3 -m pytest tests/ -q

clean:
	rm -rf $(DIST) $(BUILD) $(APPDIR) __pycache__ echocoin_chain.db echocoin_chain.db-shm echocoin_chain.db-wal
	find . -name "*.pyc" -delete
