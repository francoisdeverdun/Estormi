# iOS app (apps/estormi-ios) — TestFlight / App Store distribution.
#
# A TestFlight/App Store build resolves to the PRODUCTION CloudKit + APNs
# environment (the iOS entitlements pin no `icloud-container-environment`, so
# Xcode picks Development for Debug and Production for a Distribution archive) —
# the only way to test the distributable CloudKit doorbell against a real device.
# See docs/cloudkit-doorbell.md and docs/ios-push-notifications.md.
.PHONY: ios-testflight

# Inputs (all required; nothing team-specific is committed to source):
#   APPLE_API_KEY_ID + APPLE_API_ISSUER + APPLE_API_KEY_PATH — the same App Store
#     Connect API key `notarize` uses; here it both signs (provisioning) and
#     uploads the build.
#   APPLE_TEAM_ID — the signing team.
#   IOS_PROVISION_PROFILE — the NAME of an App Store provisioning profile for
#     `app.estormi.ios` (create it once on the portal with an Apple Distribution
#     cert). Manual signing is used because a "Developer"-role API key cannot
#     cloud-sign distribution assets ("Cloud signing permission error").
IOS_PROVISION_PROFILE ?=

ios-testflight: ## Archive + upload the iOS app to TestFlight (needs APPLE_API_* + APPLE_TEAM_ID + IOS_PROVISION_PROFILE)
	@if [ -z "$(APPLE_API_KEY_ID)" ] || [ -z "$(APPLE_API_ISSUER)" ] || [ -z "$(APPLE_API_KEY_PATH)" ]; then \
	  echo "ios-testflight: set APPLE_API_KEY_ID + APPLE_API_ISSUER + APPLE_API_KEY_PATH (App Store Connect API key)."; exit 1; \
	fi
	@if [ -z "$(APPLE_TEAM_ID)" ]; then echo "ios-testflight: set APPLE_TEAM_ID to your Apple team id."; exit 1; fi
	@if [ -z "$(IOS_PROVISION_PROFILE)" ]; then \
	  echo "ios-testflight: set IOS_PROVISION_PROFILE to your App Store provisioning profile name."; \
	  echo "                e.g.  make ios-testflight IOS_PROVISION_PROFILE='Estormi iOS App Store' \\"; \
	  echo "                        APPLE_TEAM_ID=ABCDE12345 APPLE_API_KEY_ID=… APPLE_API_ISSUER=… APPLE_API_KEY_PATH=…"; \
	  exit 1; \
	fi
	@# Generate a transient ExportOptions.plist (gitignored) from the inputs —
	@# keeps the Team ID + profile name out of committed source.
	@printf '%s\n' \
	  '<?xml version="1.0" encoding="UTF-8"?>' \
	  '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
	  '<plist version="1.0">' \
	  '<dict>' \
	  '  <key>method</key><string>app-store-connect</string>' \
	  '  <key>destination</key><string>upload</string>' \
	  '  <key>teamID</key><string>$(APPLE_TEAM_ID)</string>' \
	  '  <key>signingStyle</key><string>manual</string>' \
	  '  <key>signingCertificate</key><string>Apple Distribution</string>' \
	  '  <key>provisioningProfiles</key>' \
	  '  <dict><key>app.estormi.ios</key><string>$(IOS_PROVISION_PROFILE)</string></dict>' \
	  '</dict>' \
	  '</plist>' > apps/estormi-ios/ExportOptions.plist
	cd apps/estormi-ios && xcodegen generate
	cd apps/estormi-ios && rm -rf build/Estormi.xcarchive build/export
	@# Archive (automatic dev signing — the export step re-signs for distribution).
	cd apps/estormi-ios && xcodebuild -project Estormi.xcodeproj -scheme Estormi \
	  -configuration Release -destination 'generic/platform=iOS' \
	  -archivePath build/Estormi.xcarchive -allowProvisioningUpdates \
	  -authenticationKeyPath "$(APPLE_API_KEY_PATH)" -authenticationKeyID "$(APPLE_API_KEY_ID)" \
	  -authenticationKeyIssuerID "$(APPLE_API_ISSUER)" DEVELOPMENT_TEAM=$(APPLE_TEAM_ID) archive
	@# Export with manual App Store signing + upload to App Store Connect.
	cd apps/estormi-ios && xcodebuild -exportArchive -archivePath build/Estormi.xcarchive \
	  -exportPath build/export -exportOptionsPlist ExportOptions.plist \
	  -authenticationKeyPath "$(APPLE_API_KEY_PATH)" -authenticationKeyID "$(APPLE_API_KEY_ID)" \
	  -authenticationKeyIssuerID "$(APPLE_API_ISSUER)"
	@echo "✓ ios-testflight: uploaded. After ASC processing it appears in TestFlight — add the build to an internal test group, then install via the TestFlight app."
