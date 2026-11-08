"""Load project settings before model dependencies discover another .env."""

from pathlib import Path

from dotenv import load_dotenv

# Preserve explicitly exported variables, but initialize before importing CAMEL.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

