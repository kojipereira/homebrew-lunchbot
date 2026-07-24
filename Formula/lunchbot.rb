class Lunchbot < Formula
  include Language::Python::Virtualenv

  desc "Auto-order your weekday lunch on DoorDash, with a Mac menu-bar app"
  homepage "https://github.com/YOUR-ORG/lunchbot"
  # Point url at the GitHub Release tarball produced by build.sh, and paste its
  # sha256 (shasum -a 256 dist/lunchbot-<version>.tar.gz).
  url "https://github.com/YOUR-ORG/lunchbot/releases/download/v1.1.0/lunchbot-1.1.0.tar.gz"
  sha256 "REPLACE_WITH_TARBALL_SHA256"
  license "MIT"

  depends_on "python@3.13"
  depends_on "python-tk@3.13" # the preferences window uses Tk

  # Apple Silicon only — dd-cli (which the user installs separately) is arm64-only.
  depends_on arch: :arm64

  # Menu-bar deps. Regenerate exact versions + sha256 with:
  #   brew update-python-resources ./Formula/lunchbot.rb
  # (declares rumps + its pyobjc transitive deps for offline venv install).
  # Per policy: pin versions; do not adopt a release younger than 3 days.
  resource "pyobjc-core" do
    url "https://files.pythonhosted.org/packages/source/p/pyobjc-core/pyobjc_core-10.3.1.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "pyobjc-framework-Cocoa" do
    url "https://files.pythonhosted.org/packages/source/p/pyobjc-framework-Cocoa/pyobjc_framework_Cocoa-10.3.1.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "rumps" do
    url "https://files.pythonhosted.org/packages/source/r/rumps/rumps-0.4.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      lunchbot needs the DoorDash CLI (dd-cli), which you install yourself:
        1. Download dd-cli for Apple Silicon (ask whoever shared lunchbot with you,
           or set LUNCHBOT_DDCLI_URL).
        2. Put it on your PATH, e.g.:  mv ~/Downloads/dd-cli /opt/homebrew/bin/
        3. If macOS blocks it:  xattr -d com.apple.quarantine $(command -v dd-cli)
        4. Sign in:             dd-cli login

      Then set up lunchbot:
        lunchbot setup                 # or drive it from the menu bar:
        lunchbot install-gui-agent     # auto-start the menu-bar app at login
        lunchbot install-app           # a double-clickable Lunchbot.app in ~/Applications

      (First launch of Lunchbot.app: right-click → Open once to clear Gatekeeper.)

      Health check any time:  lunchbot doctor
    EOS
  end

  test do
    assert_match "usage: lunchbot", shell_output("#{bin}/lunchbot --help")
  end
end
