import os
from dotenv import load_dotenv

# Use absolute path to be sure
dotenv_path = r"e:\Agentloop\backend\.env"
print(f"Loading from: {dotenv_path}")
print(f"File exists: {os.path.exists(dotenv_path)}")

success = load_dotenv(dotenv_path=dotenv_path)
print(f"Load success: {success}")

print(f"SUPABASE_URL: {os.getenv('SUPABASE_URL')}")
print(f"SUPABASE_JWT_SECRET: {os.getenv('SUPABASE_JWT_SECRET')}")
print(f"SUPABASE_SERVICE_ROLE_KEY: {os.getenv('SUPABASE_SERVICE_ROLE_KEY')}")
