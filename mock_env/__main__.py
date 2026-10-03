"""Run the simulated company: python -m mock_env [--port 8700]"""

import argparse

import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8700)
args = parser.parse_args()
uvicorn.run("mock_env.app:app", host="127.0.0.1", port=args.port, log_level="warning")
