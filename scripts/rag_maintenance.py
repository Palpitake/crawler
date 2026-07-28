import json
from rag.maintenance import run_maintenance

print(json.dumps(run_maintenance(), ensure_ascii=False, indent=2))
