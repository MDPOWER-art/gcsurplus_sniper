import os
from pathlib import Path
from typing import Dict, Any
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "gcsurplus.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

# Financial & Security Settings
MAX_CAPITAL_EXPOSURE: float = float(os.getenv("MAX_CAPITAL_EXPOSURE", "3000.00"))
MIN_NET_PROFIT_THRESHOLD: float = 500.00  # CFO Strict requirement: Reject if profit < $500

# Credentials & External APIs
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
GCSURPLUS_USER: str = os.getenv("GCSURPLUS_USER", "")
GCSURPLUS_PASS: str = os.getenv("GCSURPLUS_PASS", "")

# Scraper & Execution Settings
GCSURPLUS_BASE_URL: str = "https://www.gcsurplus.ca"
GCSURPLUS_SEARCH_URL: str = f"{GCSURPLUS_BASE_URL}/mn-eng.cfm"
GCSURPLUS_LOGIN_URL: str = f"{GCSURPLUS_BASE_URL}/mn-eng.cfm?snc=gcs"

# Target Categories Registry for High-Margin Arbitrage
TARGET_REGISTRY: Dict[str, Dict[str, Any]] = {
    "Infrastructure_TI": {
        "name": "Infrastructure IT & Serveurs",
        "search_terms": ["server", "switch", "router", "cisco", "dell poweredge", "hp proliant", "storage", "san", "pdu", "rack"],
        "category_code": "IT_INFRA",
    },
    "Laboratoire_Scientifique": {
        "name": "Équipement de Laboratoire & Recherche Scientifique",
        "search_terms": ["spectrometer", "microscope", "centrifuge", "chromatograph", "laser", "incubator", "autoclave", "mass spec"],
        "category_code": "LAB_SCI",
    },
    "Medical_Veterinaire": {
        "name": "Équipement Médical & Vétérinaire",
        "search_terms": ["ultrasound", "patient monitor", "defibrillator", "x-ray", "infusion pump", "medical", "veterinary", "endoscope"],
        "category_code": "MED_VET",
    },
    "Arpentage_Ingenierie": {
        "name": "Arpentage, Métrologie & Ingénierie",
        "search_terms": ["total station", "theodolite", "rtk gps", "trimble", "leica", "oscilloscope", "spectrum analyzer", "calibrator"],
        "category_code": "SURV_ENG",
    },
}
