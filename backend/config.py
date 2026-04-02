import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, storage
from supabase import create_client, Client

load_dotenv()

# Firebase Configuration
# Expects per-environment service account or default credentials
if not firebase_admin._apps:
    try:
        # In production, use environment variables or a specific path
        cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred, {
                'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
            })
        else:
            # Fallback or default init (e.g. for simple local testing if already authed)
            firebase_admin.initialize_app(options={
                'storageBucket': os.getenv("FIREBASE_STORAGE_BUCKET")
            })
    except Exception as e:
        print(f"Warning: Firebase initialization failed: {e}")

# Supabase Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    print("Warning: Supabase credentials not found in environment variables.")

# Ai Configuration
QWEN_API_KEY = os.getenv("QWEN_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

