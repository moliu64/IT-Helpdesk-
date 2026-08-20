"""Build the local Helpdesk Chroma index."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rag.vector_store import VectorStore, load_documents

def main() -> None:
    documents = load_documents()
    if not documents:
        raise SystemExit("没有可索引的知识库或历史工单")
    count = VectorStore().rebuild(documents)
    print(f"已使用本地 BGE 建立索引，共 {count} 条文档")

if __name__ == "__main__":
    main()
