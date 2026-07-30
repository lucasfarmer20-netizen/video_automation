import shutil
from pathlib import Path

src_dir = Path("src")
backend_dir = Path("backend")

backend_dir.mkdir(exist_ok=True)

# Copy modules, skipping dashboard.py (which is replaced by FastAPI main.py)
for path in src_dir.glob("*.py"):
    if path.name == "dashboard.py":
        continue
    
    dest_path = backend_dir / path.name
    print(f"Copying {path} -> {dest_path}")
    shutil.copy2(path, dest_path)

print("Finished copying pipeline source files to backend!")
