# Native macOS bundle, release tagging, signing, notarization, doorbell.
.PHONY: tag dev install-dev bundle-python build-version set-version frontend-build rust-licenses bundle notarize doorbell doorbell-dist doorbell-notarize

## ── Release ──────────────────────────────────────────────────────────────

# Maintainer-only release cut: creates the tag and pushes it to `main`, so it
# deliberately sets the ESTORMI_ALLOW_MAIN_PUSH escape hatch the pre-push hook
# documents — the release tag is the one sanctioned direct push to the otherwise
# PR-only `main`. Run it on `main`. The README download badge is a live
# shields.io endpoint that reads the latest GitHub release, so it tracks the tag
# on its own — nothing to regenerate or commit here.
tag: ## Tag and push a release: make tag V=v1.5 (CI builds + publishes the DMG)
	@[ -n "$(V)" ] || (echo "Usage: make tag V=vX.Y"; exit 1)
	@echo "Tagging $(V)…"
	git tag $(V)
	ESTORMI_ALLOW_MAIN_PUSH=1 git push && git push origin $(V)
	@echo "✓ Tagged $(V) — the Release workflow will build and publish Estormi.dmg"

## ── Native app bundle ────────────────────────────────────────────────────

dev: ## Run Tauri app in development mode (hot reload)
	cd apps/estormi-macos && cargo tauri dev

install-dev: ## Editable-install the six first-party Python packages into .venv
	@# Bases first, then the engines. All six carry a pyproject.toml; tests and
	@# `make start` still work via the conftest/--app-dir sys.path shim, but the
	@# editable installs give real distribution metadata + import resolution.
	@.venv/bin/pip install -e packages/memory_core
	@.venv/bin/pip install -e packages/connectors
	@.venv/bin/pip install -e packages/estormi_server
	@.venv/bin/pip install -e packages/estormi_ingestion
	@.venv/bin/pip install -e packages/estormi_briefing
	@.venv/bin/pip install -e packages/estormi_distill

bundle-python: ## Download python-build-standalone + install runtime packages into python/
	@if [ -x python/bin/python3 ]; then \
	  echo "Bundled Python already present ($$(python/bin/python3 --version))."; \
	else \
	  set -e; \
	  if [ -z "$(PYTHON_STANDALONE_SHA256)" ] && [ "$$ESTORMI_TRUST_PYTHON_STANDALONE" != "1" ]; then \
	    echo "ERROR: PYTHON_STANDALONE_SHA256 is not set, so the download cannot be verified."; \
	    echo "       Set it on the CLI:  make bundle PYTHON_STANDALONE_SHA256=<sha256>"; \
	    echo "       (offline dev only) export ESTORMI_TRUST_PYTHON_STANDALONE=1 to opt out."; \
	    exit 1; \
	  fi; \
	  if [ -z "$(PYTHON_STANDALONE_SHA256)" ] && [ -n "$$CI" ]; then \
	    echo "ERROR: refusing to skip SHA256 verification under CI (CI is set)."; \
	    echo "       Pass PYTHON_STANDALONE_SHA256=<sha256>; the trust opt-out is for offline dev only."; \
	    exit 1; \
	  fi; \
	  tmp="$$(mktemp -d)"; trap 'rm -rf "$$tmp"' EXIT; \
	  archive="$$tmp/python-build-standalone.tar.gz"; \
	  echo "Downloading python-build-standalone 3.12.10..."; \
	  curl -fsSL "$(PYTHON_STANDALONE_URL)" -o "$$archive"; \
	  if [ -n "$(PYTHON_STANDALONE_SHA256)" ]; then \
	    echo "Verifying SHA256..."; \
	    echo "$(PYTHON_STANDALONE_SHA256)  $$archive" | shasum -a 256 -c -; \
	  else \
	    echo "############################################################"; \
	    echo "# WARNING: ESTORMI_TRUST_PYTHON_STANDALONE=1 — the bundled  "; \
	    echo "# Python download is UNVERIFIED. Never use for a release or "; \
	    echo "# distributed build; offline development only.              "; \
	    echo "############################################################"; \
	  fi; \
	  tar xzf "$$archive" -C .; \
	  echo "Installing runtime packages..."; \
	  python/bin/pip install --no-deps --require-hashes -r requirements/requirements-bundle.txt --quiet; \
	  echo "Installing local workspace packages..."; \
	  python/bin/pip install ./packages/memory_core --quiet; \
	  echo "Bundled Python ready: $$(du -sh python/ | cut -f1)"; \
	fi
	@# Always re-sync bundle requirements + local workspace packages so an
	@# existing python/ never ships a stale package set. memory_core MUST be a
	@# path install with --force-reinstall: `pip install --no-deps memory_core`
	@# by bare name resolves the already-installed dist as "satisfied" and skips
	@# it, so an edit that keeps the 1.0.0 version (e.g. adding a new symbol like
	@# GCAL_GROUP_TYPES) never reaches the bundle and the app crashes on import.
	@# Fail loudly on install errors — silently shipping an app with missing
	@# deps used to slip past `make bundle` because errors were redirected to
	@# /dev/null and swallowed by ``|| true``.
	python/bin/pip install --no-deps --require-hashes -r requirements/requirements-bundle.txt --quiet
	python/bin/pip install --no-deps --force-reinstall ./packages/memory_core --quiet
	@# Rewrite stale shebangs (pip baked in an absolute path that no longer
	@# matches where python/ now lives after relocation). Idempotent.
	@scripts/fix_python_shebangs.sh python >/dev/null
	@# Ad-hoc sign the real interpreter binary (python3 is a symlink → python3.12)
	@# as app.estormi.local. The final bundle codesign in `bundle` re-signs it
	@# (and everything) with the real identity when one is present; this keeps a
	@# valid signature in the dev/no-cert case.
	@codesign --force --sign - --identifier app.estormi.local python/bin/python3.12 2>/dev/null || true

build-version: ## Write current git tag/commit for the bundled UI footer
	@v="$$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)"; \
	  printf "%s\n" "$$v" > packages/estormi_server/build_version.txt; \
	  echo "Build version: $$v"

set-version: ## Set the macOS app version everywhere: make set-version V=X.Y.Z
	@[ -n "$(V)" ] || (echo "Usage: make set-version V=X.Y.Z"; exit 1)
	$(VENV) scripts/set_version.py "$(V)"

frontend-build: ## Build the Ars Memoriae SPA (Vite) → packages/web-ui/dist/.
	@echo "Building Ars Memoriae SPA (Vite) → packages/web-ui/dist/ ..."
	@if [ ! -d node_modules ]; then \
	  pnpm install --frozen-lockfile; \
	fi
	@pnpm --filter @estormi/web-ui build

rust-licenses: ## Regenerate apps/estormi-macos/THIRD-PARTY-RUST.md from Cargo.lock (cargo-about)
	@# MIT/Apache crates linked into the shipped binary require their notices
	@# accompany it (NOTICE points here). `bundle` collects the committed
	@# THIRD-PARTY-RUST.md into the .app, so a builder without cargo-about still
	@# ships the attribution; this target refreshes the committed baseline after a
	@# Cargo.lock change. Install once with: cargo install cargo-about
	@command -v cargo-about >/dev/null 2>&1 || { echo "rust-licenses: cargo-about not installed — run: cargo install cargo-about"; exit 1; }
	cd apps/estormi-macos && cargo about generate about.hbs -o THIRD-PARTY-RUST.md
	@echo "✓ rust-licenses: apps/estormi-macos/THIRD-PARTY-RUST.md regenerated — commit it."

bundle: bundle-python build-version frontend-build ## Build distributable Estormi.zip (signed if a codesigning identity is present, else ad-hoc)
	@echo "Step 1: Generating icons from estormi-app-icon.png..."
	@bash scripts/generate_icons.sh
	@echo "Step 2: Building Tauri app (unsigned — we sign as the last step below)..."
	@# The Tauri `devtools` feature is intentionally not enabled (see Cargo.toml),
	@# so this release build ships without the WebView inspector. We let Tauri
	@# build UNSIGNED and sign the finished bundle ourselves afterwards — Tauri v2
	@# would otherwise apply the hardened runtime, and the hardened runtime stops
	@# the bundled Python sidecar from presenting the macOS Contacts permission
	@# prompt (the child process can no longer borrow the app's usage strings).
	cd apps/estormi-macos && cargo tauri build --target aarch64-apple-darwin
	@# Tauri copied the dev-time python/ tree verbatim into the bundle, so
	@# every script's shebang still points at the developer's repo path.
	@# Rewrite them to the install location the bundle will live at.
	@bundle_py="apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos/Estormi.app/Contents/Resources/_up_/_up_/python"; \
	  if [ -d "$$bundle_py" ]; then \
	    INSTALL_ROOT="/Applications/Estormi.app/Contents/Resources/_up_/_up_/python" \
	      scripts/fix_python_shebangs.sh "$$bundle_py" >/dev/null; \
	  fi
	@# Collect every bundled wheel's license text into the .app so the
	@# redistribution carries it (NOTICE points readers here). Done BEFORE the
	@# signing step below so the added files land inside the sealed bundle. A
	@# wheel that ships no LICENSE*/COPYING*/NOTICE*/AUTHORS* in its dist-info
	@# (odfpy, mistral_common, sentencepiece, openpyxl, tokenizers, onnxruntime, …)
	@# falls back to its METADATA license declaration, then is overlaid with any
	@# committed verbatim text from make/extra-licenses/<pkg>/ (the LGPL-2.1 text
	@# odfpy needs) — so no bundled package is silently omitted, and a contract
	@# test (tests/contract/test_bundle_license_collection.py) pins the invariant.
	@app="apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos/Estormi.app"; \
	  sp="$$app/Contents/Resources/_up_/_up_/python/lib/python3.12/site-packages"; \
	  dest="$$app/Contents/Resources/THIRD-PARTY-LICENSES"; \
	  if [ -d "$$sp" ]; then \
	    rm -rf "$$dest"; mkdir -p "$$dest"; \
	    for di in "$$sp"/*.dist-info; do \
	      [ -d "$$di" ] || continue; \
	      name="$$(basename "$$di" .dist-info)"; \
	      found=0; \
	      for lic in "$$di"/LICENSE* "$$di"/COPYING* "$$di"/NOTICE* "$$di"/AUTHORS*; do \
	        [ -e "$$lic" ] && { mkdir -p "$$dest/$$name"; cp "$$lic" "$$dest/$$name/"; found=1; }; \
	      done; \
	      [ -d "$$di/licenses" ] && { mkdir -p "$$dest/$$name"; cp -R "$$di/licenses/." "$$dest/$$name/"; found=1; }; \
	      if [ "$$found" = 0 ] && [ -e "$$di/METADATA" ]; then \
	        mkdir -p "$$dest/$$name"; cp "$$di/METADATA" "$$dest/$$name/METADATA-LICENSE.txt"; \
	      fi; \
	    done; \
	    if [ -d make/extra-licenses ]; then \
	      for ov in make/extra-licenses/*/; do \
	        [ -d "$$ov" ] || continue; ovname="$$(basename "$$ov")"; \
	        for d in "$$dest/$$ovname"-*/ "$$dest/$$ovname"/; do \
	          [ -d "$$d" ] && cp -R "$$ov"/. "$$d"/; \
	        done; \
	      done; \
	    fi; \
	    cpy_lic="$$app/Contents/Resources/_up_/_up_/python/lib/python3.12/LICENSE.txt"; \
	    [ -e "$$cpy_lic" ] && { mkdir -p "$$dest/cpython"; cp "$$cpy_lic" "$$dest/cpython/"; echo "  Collected CPython interpreter LICENSE → THIRD-PARTY-LICENSES/cpython/"; }; \
	    rust_lic="apps/estormi-macos/THIRD-PARTY-RUST.md"; \
	    [ -e "$$rust_lic" ] && { mkdir -p "$$dest/rust"; cp "$$rust_lic" "$$dest/rust/"; echo "  Collected Rust crate licenses → THIRD-PARTY-LICENSES/rust/"; } || echo "  WARNING: $$rust_lic missing — run 'make rust-licenses' to regenerate the Rust attribution."; \
	    echo "  Collected third-party licenses → THIRD-PARTY-LICENSES/ ($$(find "$$dest" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') entries)"; \
	  fi
	@# Embed the distributable CloudKit doorbell helper, if built by `make
	@# doorbell-dist` (+ `doorbell-notarize`), so download users get new-briefing
	@# pushes with no setup — the Rust shell extracts it to the config home on first
	@# run (see apps/estormi-macos/src/doorbell.rs). CONDITIONAL: `make bundle` with
	@# no built helper just skips this (the doorbell then needs a separate `make
	@# doorbell`). Done BEFORE signing so the payload is sealed into the parent.
	@# Shipped as a ZIP (opaque data the parent just hashes), NOT a loose nested
	@# .app (whose nested-code signing is version-fragile); the helper keeps its own
	@# Developer ID signature + stapled ticket inside the zip. The team pin written
	@# into the bundled config is read FROM the helper's own signature, never a
	@# committed-source constant, so it always matches the signer the doorbell trusts.
	@app="apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos/Estormi.app"; \
	  helper="$(DOORBELL_BUILT_APP)"; res="$$app/Contents/Resources"; \
	  if [ -d "$$helper" ]; then \
	    rm -f "$$res/EstormiCloud.app.zip"; \
	    ditto -c -k --keepParent "$$helper" "$$res/EstormiCloud.app.zip"; \
	    plutil -extract CFBundleVersion raw "$$helper/Contents/Info.plist" > "$$res/EstormiCloud.version" 2>/dev/null || true; \
	    team="$$(codesign -dv "$$helper" 2>&1 | sed -n 's/^TeamIdentifier=//p')"; \
	    printf '{"team_id":"%s","enabled":true}\n' "$$team" > "$$res/doorbell_config.json"; \
	    echo "  Embedded doorbell helper (zip) + team-pinned config (team $$team)."; \
	  else \
	    echo "  No doorbell-dist helper built — skipping doorbell embed (run 'make doorbell-dist && make doorbell-notarize' to include it)."; \
	  fi
	@# Sign the FINISHED bundle (after every mutation, so the seal stays valid)
	@# with CODESIGN_ID, WITHOUT the hardened runtime. macOS keys TCC grants on
	@# the signature's *designated requirement* — cert + bundle id, not the cdhash
	@# — so the grants (Full Disk Access, and the sidecar's Contacts/Calendar/
	@# Reminders) persist across rebuilds. We sign the bundled Python interpreter
	@# explicitly: --deep does NOT reach a Mach-O buried under Resources/, and a
	@# cert-signed interpreter is what matches the bundle-id Contacts grant. We
	@# omit `--options runtime`: the hardened runtime stops the sidecar from
	@# presenting the Contacts prompt, and is only needed for notarization (which
	@# this direct-distribution build skips). Sign inside-out: interpreter first,
	@# then the app bundle that seals it.
	@app="apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos/Estormi.app"; \
	  py="$$app/Contents/Resources/_up_/_up_/python/bin/python3.12"; \
	  if [ -n "$(CODESIGN_ID)" ]; then \
	    echo "  Signing bundled Python + app with $(CODESIGN_ID) (no hardened — grants persist, Contacts prompt + names work)."; \
	    codesign --force --sign "$(CODESIGN_ID)" --identifier app.estormi.local "$$py" 2>/dev/null || true; \
	    codesign --force --sign "$(CODESIGN_ID)" "$$app"; \
	  else \
	    echo "  No codesigning identity — leaving the bundle ad-hoc (grants reset each rebuild)."; \
	  fi
	@echo "Step 3: Packaging distribution zip..."
	@mkdir -p dist
	@rm -f "$(CURDIR)/dist/Estormi.zip"
	@# Stage install.sh next to Estormi.app so the archive stores it as a plain
	@# `install.sh` entry — without staging, zip would embed the absolute
	@# `$(CURDIR)/scripts/install.sh` path verbatim into the archive, leaking
	@# the maintainer's home directory to every downloader.
	@cp "$(CURDIR)/scripts/install.sh" "apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos/install.sh"
	cd "apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos" && \
	    zip -r "$(CURDIR)/dist/Estormi.zip" Estormi.app install.sh
	@echo ""
	@echo "✓ Bundle: dist/Estormi.zip"
	@# `make bundle` is build-only: it produces the .app and dist/Estormi.zip
	@# but does NOT kill the running app or install to /Applications. The
	@# install step (kill → staged atomic swap → relaunch → health-check) is
	@# owned solely by scripts/build.sh, which is the single rebuild +
	@# install entrypoint. Keeping the two responsibilities separate
	@# avoids the app being installed twice per run.

# Apple notarization credentials. Notarization is OPTIONAL and OFF by default:
# the `notarize` target only does real work when ALL of these are set (a
# Developer ID Application cert via APPLE_SIGNING_IDENTITY, plus notarytool
# credentials). Absent any of them it prints how to enable it and exits 0, so
# CI and credential-less developers are never broken. Provide either an
# App-Store-Connect API key (APPLE_API_KEY_ID + APPLE_API_ISSUER + a .p8 at
# APPLE_API_KEY_PATH) or an Apple-ID app-specific password (APPLE_ID +
# APPLE_TEAM_ID + APPLE_APP_PASSWORD).
APPLE_API_KEY_ID    ?=
APPLE_API_ISSUER    ?=
APPLE_API_KEY_PATH  ?=
APPLE_ID            ?=
APPLE_TEAM_ID       ?=
APPLE_APP_PASSWORD  ?=

# Notarization mandates the hardened runtime, so this target re-signs the
# bundle inside-out (interpreter first, then the app) with the same identity +
# entitlements as `bundle` but adding `--options runtime` — the default
# `bundle` build omits it so the Contacts prompt and TCC grants keep working
# for direct, un-notarized install. After a successful notarytool run it
# staples the ticket so the app passes Gatekeeper offline.
notarize: ## Notarize + staple dist Estormi.app (no-op unless Apple creds are set)
	@app="apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos/Estormi.app"; \
	py="$$app/Contents/Resources/_up_/_up_/python/bin/python3.12"; \
	if [ -z "$(CODESIGN_ID)" ]; then \
	  echo "notarize: skipped — no Developer ID Application cert (set APPLE_SIGNING_IDENTITY)."; \
	  echo "          Notarization requires a real Apple cert; ad-hoc signatures cannot be notarized."; \
	  exit 0; \
	fi; \
	if [ ! -d "$$app" ]; then \
	  echo "notarize: skipped — no built bundle at $$app. Run 'make bundle' first."; \
	  exit 0; \
	fi; \
	if [ -n "$(APPLE_API_KEY_ID)" ] && [ -n "$(APPLE_API_ISSUER)" ] && [ -n "$(APPLE_API_KEY_PATH)" ]; then \
	  cred_args="--key $(APPLE_API_KEY_PATH) --key-id $(APPLE_API_KEY_ID) --issuer $(APPLE_API_ISSUER)"; \
	elif [ -n "$(APPLE_ID)" ] && [ -n "$(APPLE_TEAM_ID)" ] && [ -n "$(APPLE_APP_PASSWORD)" ]; then \
	  cred_args="--apple-id $(APPLE_ID) --team-id $(APPLE_TEAM_ID) --password $(APPLE_APP_PASSWORD)"; \
	else \
	  echo "notarize: skipped — Apple creds not set."; \
	  echo "          Set either APPLE_API_KEY_ID + APPLE_API_ISSUER + APPLE_API_KEY_PATH (API key),"; \
	  echo "          or APPLE_ID + APPLE_TEAM_ID + APPLE_APP_PASSWORD (Apple-ID app password)."; \
	  exit 0; \
	fi; \
	echo "notarize: re-signing WITH the hardened runtime (notarization requires it)..."; \
	codesign --force --options runtime --sign "$(CODESIGN_ID)" \
	  --entitlements apps/estormi-macos/Estormi.entitlements --identifier app.estormi.local "$$py"; \
	codesign --force --options runtime --sign "$(CODESIGN_ID)" \
	  --entitlements apps/estormi-macos/Estormi.entitlements "$$app"; \
	echo "notarize: zipping + submitting to notarytool (this can take minutes)..."; \
	ditto -c -k --keepParent "$$app" "$$app.zip"; \
	xcrun notarytool submit "$$app.zip" $$cred_args --wait; \
	echo "notarize: stapling ticket onto the bundle..."; \
	xcrun stapler staple "$$app"; \
	rm -f "$$app.zip"; \
	echo "✓ notarize: Estormi.app notarized + stapled. Re-run 'make bundle' packaging or re-zip dist/Estormi.zip."

# ── CloudKit doorbell helper ──────────────────────────────────────────────────
# EstormiCloud.app is the CloudKit entitlements bearer (a bare executable
# cannot claim restricted entitlements — TN3125). It is installed OUTSIDE the
# Estormi.app bundle, into $ESTORMI_DATA_DIR/bin, so the parent bundle's
# signature seal is never broken and no --deep re-sign can ever strip the
# helper's entitlements. Automatic signing via -allowProvisioningUpdates lets
# Xcode create the App ID, the iCloud container and the provisioning profile
# on the fly — requires DOORBELL_TEAM (defaults to APPLE_TEAM_ID) and Xcode
# logged into the Apple Developer account. NOT part of `bundle`: the doorbell
# is opt-in (see docs/cloudkit-doorbell.md).
DOORBELL_TEAM ?= $(APPLE_TEAM_ID)

doorbell: ## Build + install the CloudKit doorbell helper (needs DOORBELL_TEAM)
	@if [ -z "$(DOORBELL_TEAM)" ]; then \
	  echo "doorbell: set DOORBELL_TEAM (or APPLE_TEAM_ID) to your Apple team id."; \
	  echo "          e.g.  make doorbell DOORBELL_TEAM=ABCDE12345"; \
	  exit 1; \
	fi
	cd apps/estormi-cloud && xcodegen generate
	@# DerivedData (not build/) — project.yml writes its generated Info.plist
	@# under build/, and pointing -derivedDataPath there would collide with it.
	@# Device registration: Mac *Development* profiles are device-scoped, so
	@# the building Mac itself must be registered with the team on first build.
	cd apps/estormi-cloud && xcodebuild -project EstormiCloud.xcodeproj \
	  -scheme EstormiCloud -configuration Release -derivedDataPath DerivedData \
	  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
	  DEVELOPMENT_TEAM=$(DOORBELL_TEAM) build
	@# Install into the config home (never relocates with the library), NOT the
	@# movable data dir — keyed on ESTORMI_CONFIG_HOME, mirroring datadir.config_home().
	@dest="$${ESTORMI_CONFIG_HOME:-$$HOME/Library/Application Support/Estormi}/bin"; \
	mkdir -p "$$dest"; \
	rm -rf "$$dest/EstormiCloud.app"; \
	ditto "apps/estormi-cloud/DerivedData/Build/Products/Release/EstormiCloud.app" \
	  "$$dest/EstormiCloud.app"; \
	codesign --verify --deep --strict "$$dest/EstormiCloud.app"; \
	"$$dest/EstormiCloud.app/Contents/MacOS/EstormiCloud" --status; rc=$$?; \
	if [ $$rc -ne 0 ] && [ $$rc -ne 2 ]; then \
	  echo "doorbell: helper failed its smoke test (exit $$rc) — see output above."; \
	  exit 1; \
	fi; \
	echo "✓ doorbell: EstormiCloud.app installed at $$dest (exit $$rc; 2 = no iCloud session yet)."

# ── Distributable doorbell helper (Developer ID + Production + notarized) ──────
# `doorbell` above produces an Apple-DEVELOPMENT, device-locked, CloudKit-
# Development helper — perfect for the maintainer's own dev iPhone, useless for
# users who download the app. `doorbell-dist` produces the DISTRIBUTION artifact:
# a Developer ID Application-signed, hardened-runtime, CloudKit-PRODUCTION helper
# that runs on any Mac and (once notarized by `doorbell-notarize`) passes
# Gatekeeper offline. It is consumed by `bundle` (embedded into Estormi.app) and
# auto-installed to the config home on first run. See docs/cloudkit-doorbell.md.
#
# Inputs: CODESIGN_ID resolves a "Developer ID Application" cert (override with
# APPLE_SIGNING_IDENTITY); DOORBELL_TEAM (defaults to APPLE_TEAM_ID); and
# DOORBELL_PROVISION_PROFILE — the name of a *Developer ID* provisioning profile
# that authorizes the iCloud.app.estormi.ios container in Production (installed in
# ~/Library/MobileDevice/Provisioning Profiles/). The committed entitlements stay
# on Development for the dev path; this target generates a transient Production
# copy and selects it via CODE_SIGN_ENTITLEMENTS (the env lives in the plist, not
# a build setting — so the CLI override is the working mechanism).
DOORBELL_SIGN_ID        ?= Developer ID Application
DOORBELL_PROVISION_PROFILE ?=
DOORBELL_DIST_ENTITLEMENTS := apps/estormi-cloud/Sources/EstormiCloud.production.entitlements
DOORBELL_BUILT_APP := apps/estormi-cloud/DerivedData/Build/Products/Release/EstormiCloud.app

doorbell-dist: ## Build the Developer ID + Production helper for distribution (needs DOORBELL_PROVISION_PROFILE)
	@if [ -z "$(CODESIGN_ID)" ]; then \
	  echo "doorbell-dist: no Developer ID Application cert found (set APPLE_SIGNING_IDENTITY)."; exit 1; \
	fi
	@if [ -z "$(DOORBELL_PROVISION_PROFILE)" ]; then \
	  echo "doorbell-dist: set DOORBELL_PROVISION_PROFILE to the name of your Developer ID"; \
	  echo "               provisioning profile authorizing iCloud.app.estormi.ios in Production."; \
	  echo "               e.g.  make doorbell-dist DOORBELL_PROVISION_PROFILE='Estormi Doorbell Developer ID'"; \
	  exit 1; \
	fi
	@if [ -z "$(DOORBELL_TEAM)" ]; then \
	  echo "doorbell-dist: set DOORBELL_TEAM (or APPLE_TEAM_ID) to your Apple team id —"; \
	  echo "               xcodebuild needs DEVELOPMENT_TEAM to match the provisioning profile."; \
	  echo "               e.g.  make doorbell-dist DOORBELL_TEAM=ABCDE12345 DOORBELL_PROVISION_PROFILE='…'"; \
	  exit 1; \
	fi
	@# Transient Production entitlements: copy the committed Development file and
	@# flip the one environment string (the only "Development" occurrence in it).
	sed 's,<string>Development</string>,<string>Production</string>,' \
	  apps/estormi-cloud/Sources/EstormiCloud.entitlements > "$(DOORBELL_DIST_ENTITLEMENTS)"
	@grep -q "<string>Production</string>" "$(DOORBELL_DIST_ENTITLEMENTS)" || \
	  { echo "doorbell-dist: failed to set Production env in entitlements"; exit 1; }
	cd apps/estormi-cloud && xcodegen generate
	@# Manual Developer ID signing. NO -allowProvisioningUpdates/-DeviceRegistration
	@# (those force the device-scoped automatic Development profile we are escaping).
	@# --timestamp is required for notarization; hardened runtime comes from project.yml.
	@# CODE_SIGN_INJECT_BASE_ENTITLEMENTS=NO: a plain `xcodebuild build` (vs
	@# `archive`) injects `com.apple.security.get-task-allow=true` even in Release,
	@# which notarization rejects. Disabling base-entitlement injection signs with
	@# exactly our Production entitlements file (no get-task-allow); the profile
	@# still supplies application-identifier + team-identifier.
	cd apps/estormi-cloud && xcodebuild -project EstormiCloud.xcodeproj \
	  -scheme EstormiCloud -configuration Release -derivedDataPath DerivedData \
	  CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY="$(DOORBELL_SIGN_ID)" \
	  PROVISIONING_PROFILE_SPECIFIER="$(DOORBELL_PROVISION_PROFILE)" \
	  CODE_SIGN_ENTITLEMENTS=Sources/EstormiCloud.production.entitlements \
	  CODE_SIGN_INJECT_BASE_ENTITLEMENTS=NO \
	  DEVELOPMENT_TEAM=$(DOORBELL_TEAM) OTHER_CODE_SIGN_FLAGS="--timestamp" build
	@# Assert the build is what we shipped for: Developer ID authority, hardened
	@# runtime, Production env, and NO get-task-allow (notarization rejects it).
	@codesign --verify --strict --verbose=2 "$(DOORBELL_BUILT_APP)"
	@# Read entitlements as XML (the default codesign format prints [Key]/[Value],
	@# not the literal <string>…</string> we assert on).
	@codesign -d --entitlements - --xml "$(DOORBELL_BUILT_APP)" 2>/dev/null | grep -q "<string>Production</string>" \
	  || { echo "doorbell-dist: built helper is NOT pinned to CloudKit Production — aborting"; exit 1; }
	@codesign -d --entitlements - --xml "$(DOORBELL_BUILT_APP)" 2>/dev/null | grep -q "get-task-allow" \
	  && { echo "doorbell-dist: built helper carries get-task-allow (debuggable) — would fail notarization"; exit 1; } || true
	@codesign -dvvv "$(DOORBELL_BUILT_APP)" 2>&1 | grep -q "Authority=Developer ID Application" \
	  || { echo "doorbell-dist: built helper is not Developer ID-signed — aborting"; exit 1; }
	@echo "✓ doorbell-dist: $(DOORBELL_BUILT_APP) (Developer ID, hardened, CloudKit Production)."

doorbell-notarize: ## Notarize + staple the doorbell-dist helper (submit + staple ONLY, never re-sign)
	@app="$(DOORBELL_BUILT_APP)"; \
	if [ ! -d "$$app" ]; then echo "doorbell-notarize: no built helper at $$app. Run 'make doorbell-dist' first."; exit 1; fi; \
	if [ -n "$(APPLE_API_KEY_ID)" ] && [ -n "$(APPLE_API_ISSUER)" ] && [ -n "$(APPLE_API_KEY_PATH)" ]; then \
	  cred_args="--key $(APPLE_API_KEY_PATH) --key-id $(APPLE_API_KEY_ID) --issuer $(APPLE_API_ISSUER)"; \
	elif [ -n "$(APPLE_ID)" ] && [ -n "$(APPLE_TEAM_ID)" ] && [ -n "$(APPLE_APP_PASSWORD)" ]; then \
	  cred_args="--apple-id $(APPLE_ID) --team-id $(APPLE_TEAM_ID) --password $(APPLE_APP_PASSWORD)"; \
	else \
	  echo "doorbell-notarize: skipped — Apple notary creds not set."; \
	  echo "          Set APPLE_API_KEY_ID + APPLE_API_ISSUER + APPLE_API_KEY_PATH (API key),"; \
	  echo "          or APPLE_ID + APPLE_TEAM_ID + APPLE_APP_PASSWORD (Apple-ID app password)."; \
	  exit 0; \
	fi; \
	echo "doorbell-notarize: zipping + submitting (the helper is ALREADY signed by doorbell-dist; we never re-sign)..."; \
	ditto -c -k --keepParent "$$app" "$$app.zip"; \
	xcrun notarytool submit "$$app.zip" $$cred_args --wait; \
	xcrun stapler staple "$$app"; \
	rm -f "$$app.zip"; \
	xcrun stapler validate "$$app"; \
	codesign -dv --entitlements - "$$app" 2>&1 | grep -q "Production" \
	  || { echo "doorbell-notarize: Production env lost after stapling — aborting"; exit 1; }; \
	echo "✓ doorbell-notarize: helper notarized + stapled (signature + Production env intact)."

