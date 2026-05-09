import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv('e:/Agentloop/backend/.env')

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

client = create_client(url, key)

print("Backfilling user_id for agent_runs based on workspace_id...")
try:
    # Get all workspaces
    ws_res = client.table("workspaces").select("id, user_id").execute()
    workspaces = ws_res.data
    ws_map = {ws['id']: ws['user_id'] for ws in workspaces}
    
    # Get all agent_runs with None user_id and not None workspace_id
    runs_res = client.table("agent_runs").select("id, workspace_id").is_("user_id", "null").not_.is_("workspace_id", "null").execute()
    runs = runs_res.data
    
    count = 0
    for r in runs:
        ws_id = r.get('workspace_id')
        if ws_id in ws_map:
            user_id = ws_map[ws_id]
            client.table("agent_runs").update({"user_id": user_id}).eq("id", r['id']).execute()
            count += 1
            
    print(f"Updated {count} agent_runs.")

    # Also backfill uploaded_files (if possible, though I saw it wasn't in schema cache earlier)
    try:
        files_res = client.table("uploaded_files").select("id, workspace_id").is_("user_id", "null").not_.is_("workspace_id", "null").execute()
        files = files_res.data
        fc = 0
        for f in files:
            ws_id = f.get('workspace_id')
            if ws_id in ws_map:
                user_id = ws_map[ws_id]
                client.table("uploaded_files").update({"user_id": user_id}).eq("id", f['id']).execute()
                fc += 1
        print(f"Updated {fc} uploaded_files.")
    except Exception as e:
        print("uploaded_files backfill error:", e)

except Exception as e:
    print("Error:", e)
