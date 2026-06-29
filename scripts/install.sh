#!/bin/bash
# Install Estormi.app on macOS.
# Run this after unzipping the distribution archive:
#   unzip Estormi.zip -d /tmp/estormi && bash /tmp/estormi/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing Estormi..."

# Strip the quarantine xattr Gatekeeper sets on downloaded apps. This is a
# DELIBERATE, assumed Gatekeeper workaround: Estormi is currently distributed
# un-notarized, so without this the app would be blocked ("cannot be opened
# because the developer cannot be verified") on first launch. Once the bundle
# is notarized + stapled (see the Makefile `notarize` target), Gatekeeper
# accepts it without stripping quarantine and this line can be dropped.
xattr -cr "$DIR/Estormi.app"

# Copy to Applications, replacing any previous version
if [ -d "/Applications/Estormi.app" ]; then
  echo "Removing previous version..."
  rm -rf "/Applications/Estormi.app"
fi

cp -r "$DIR/Estormi.app" /Applications/

echo "✓ Estormi installed to /Applications/Estormi.app"
echo ""
echo "Opening Estormi..."
open /Applications/Estormi.app
echo ""
echo "Look for the Estormi icon in your Applications folder."
