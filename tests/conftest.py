import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "lane-keeper"

if "lane_keeper" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "lane_keeper", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lane_keeper"] = mod
    spec.loader.exec_module(mod)
