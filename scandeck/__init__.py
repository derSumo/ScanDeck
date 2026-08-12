"""ScanDeck: eSCL scan orchestration and Paperless-ngx upload.

The package holds everything that can be reasoned about on its own — talking to
the scanner, to Paperless, to the filesystem. The Flask application in app.py
wires those parts together and owns the HTTP surface.
"""

from scandeck.version import APP_VERSION

__all__ = ["APP_VERSION"]
