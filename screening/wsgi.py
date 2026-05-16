"""
WSGI entry point for PythonAnywhere deployment.

Usage on PythonAnywhere:
1. Upload the entire 'screening/' folder to /home/YOUR_USERNAME/screening/
2. Go to Web tab → Add new web app → Manual configuration → Python 3.10+
3. In the WSGI config file, replace content with the import below:

     import sys
     path = '/home/YOUR_USERNAME/screening'
     if path not in sys.path:
         sys.path.insert(0, path)
     from app import app as application

4. In "Virtualenv" section, ensure Flask is installed:
     pip install --user flask
5. Reload web app.

This wsgi.py is here as a reference — PythonAnywhere uses its own WSGI file.
"""
import os
import sys

# Add the screening directory to Python path
_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from app import app as application  # noqa: E402

# For local testing: `python wsgi.py`
if __name__ == "__main__":
    application.run(host="0.0.0.0", port=5050, debug=False)
