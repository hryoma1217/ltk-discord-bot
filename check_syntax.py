import ast
from pathlib import Path

for file_name in ["bot.py", "storage.py"]:
    ast.parse(Path(file_name).read_text(encoding="utf-8"))

print("ok")
