"""GUI layer: a rumps menu-bar app (app.py) + an AppKit preferences window
(prefs.py, a stub that lazily imports prefs_window.py, run as a separate
process). Both are optional — the scheduled ordering path never imports this
package, so a GUI/venv failure can't stop lunch from being ordered."""
