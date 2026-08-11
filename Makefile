# PoolCoin build targets
#
# make linux   -- build dist/poolcoin         (run on Linux)
# make windows -- build dist/poolcoin.exe     (run on Windows)
# make clean   -- remove build artifacts
#
# PyInstaller must build on the target platform. You cannot cross-compile.
# GENESIS_TIMESTAMP and HEIGHT_TOLERANCE are set directly in params.py
# and committed to the repo -- no patching needed at build time.
#
# Requirements: pip install pyinstaller

SPEC  = poolcoin.spec
DIST  = dist
BUILD = build

.PHONY: linux windows clean

linux:
	pyinstaller --clean --noconfirm $(SPEC)
	@echo "Built: $(DIST)/poolcoin"

windows:
	pyinstaller --clean --noconfirm $(SPEC)
	@echo "Built: $(DIST)/poolcoin.exe"

clean:
	rm -rf $(DIST) $(BUILD) __pycache__
	find . -name "*.pyc" -delete
