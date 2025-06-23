import os
import json
import logging
import firebase_admin
from firebase_admin import credentials
from supabase import create_client, Client
from dotenv import load_dotenv

# variables desde .env
load_dotenv()

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuración de Supabase ---
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')

if not supabase_url or not supabase_key:
    raise ValueError("Las variables de entorno de Supabase no están configuradas correctamente.")

supabase: Client = create_client(supabase_url, supabase_key)
logger.info("Cliente de Supabase inicializado con éxito en modo de servicio.")

# --- Configuración de Firebase ---
try:
    firebase_service_account_json_str = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON')
    if firebase_service_account_json_str:
        firebase_service_account_dict = json.loads(firebase_service_account_json_str)
        cred = credentials.Certificate(firebase_service_account_dict)
        firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK inicializado con éxito.")
    else:
        logger.warning("Variable de entorno de Firebase no encontrada. Las notificaciones no funcionarán.")
except Exception as e:
    logger.error(f"Error al inicializar Firebase Admin SDK: {e}", exc_info=True)

# --- Configuración de Stripe ---
import stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
webhook_secret = os.getenv('STRIPE_WEBHOOK_SIGNING_SECRET')