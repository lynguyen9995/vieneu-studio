"""Run the adapter with fixed loopback binding and one worker."""
from pathlib import Path
import uvicorn
from gateway.config import load_settings
from gateway.app import create_app


def main():
    root = Path(__file__).resolve().parents[1]
    settings = load_settings(root)
    app = create_app(settings)
    uvicorn.run(app, host='127.0.0.1', port=8001, workers=1, log_level='info')


if __name__ == '__main__':
    main()
