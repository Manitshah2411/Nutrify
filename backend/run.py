from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).with_name('.env'), override=False)

import os
from app import create_app

app = create_app()

if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5000'))
    debug = os.environ.get('FLASK_DEBUG', '0').lower() in {'1', 'true', 'yes'}
    app.run(host=host, port=port, debug=debug)

