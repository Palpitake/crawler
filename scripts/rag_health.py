import json
from rag import get_rag_service

print(json.dumps(get_rag_service().health(), ensure_ascii=False, indent=2))
