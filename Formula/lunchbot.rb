class Lunchbot < Formula
  include Language::Python::Virtualenv

  desc "Auto-order your weekday lunch on DoorDash, with a Mac menu-bar app"
  homepage "https://github.com/kojipereira/homebrew-lunchbot"
  url "https://github.com/kojipereira/homebrew-lunchbot/releases/download/v1.1.5/lunchbot-1.1.5.tar.gz"
  sha256 "1c57c98e63f19b0283cd02e3e5e2fa6e17d578ed774c43b3d074f096276ddcfe"
  license "MIT"

  depends_on "python@3.13"
  depends_on "python-tk@3.13" # the preferences window uses Tk
  depends_on arch: :arm64     # dd-cli (user-supplied) is arm64-only

  # Menu-bar deps (rumps + its pyobjc runtime). Pinned to long-stable releases.
  # Refresh with:  brew update-python-resources ./Formula/lunchbot.rb
  resource "pyobjc-core" do
    url "https://files.pythonhosted.org/packages/b4/b1/729f7458a63758bd21716648a8abcd9a0c8f2d2e9897763c8a1a1c7fd31b/pyobjc_core-12.2.1.tar.gz"
    sha256 "7a7b9b018402342cf32bf1956366896350fbe5c0478cb3ef59778f77abed7f07"
  end

  resource "pyobjc-framework-Cocoa" do
    url "https://files.pythonhosted.org/packages/51/34/fbe38a204643aa4e1b91391cdce07a34da565a69171ebcad08de7438a556/pyobjc_framework_cocoa-12.2.1.tar.gz"
    sha256 "b94b37fe5730e5ae1fb0052912cd174e6ec329b0bfba4a012ae5db1014b5864b"
  end

  resource "rumps" do
    url "https://files.pythonhosted.org/packages/b2/e2/2e6a47951290bd1a2831dcc50aec4b25d104c0cf00e8b7868cbd29cf3bfe/rumps-0.4.0.tar.gz"
    sha256 "17fb33c21b54b1e25db0d71d1d793dc19dc3c0b7d8c79dc6d833d0cffc8b1596"
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

      (First launch of Lunchbot.app: right-click -> Open once to clear Gatekeeper.)

      Health check any time:  lunchbot doctor
    EOS
  end

  test do
    assert_match "usage: lunchbot", shell_output("#{bin}/lunchbot --help")
  end
end
