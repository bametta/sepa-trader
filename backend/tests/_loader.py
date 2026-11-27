"""Loads app modules by path with heavy dependencies stubbed, so tests run without the full stack."""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")


def _new_package():
    pkg = types.ModuleType("app")
    pkg.__path__ = [APP_DIR]
    sys.modules["app"] = pkg
    return pkg


def load(name: str, stubs: dict | None = None):
    """Load app/<name>.py as app.<name>. `stubs` maps module name -> module/object to inject."""
    pkg = sys.modules.get("app") or _new_package()
    for mod_name, obj in (stubs or {}).items():
        sys.modules[mod_name] = obj
        if mod_name.startswith("app."):
            setattr(pkg, mod_name.split(".", 1)[1], obj)
    spec = importlib.util.spec_from_file_location(f"app.{name}", os.path.join(APP_DIR, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"app.{name}"] = mod
    setattr(pkg, name, mod)
    spec.loader.exec_module(mod)
    return mod


def sql_stub():
    """sqlalchemy stub where text(sql) returns the SQL string, so fake sessions can branch on it."""
    sa = types.ModuleType("sqlalchemy")
    sa.text = lambda s: s
    orm = types.ModuleType("sqlalchemy.orm")
    orm.Session = object
    return {"sqlalchemy": sa, "sqlalchemy.orm": orm}


def load_manager():
    _new_package()
    utils = load("utils")
    stubs = {**sql_stub(), "app.alpaca_client": MagicMock(), "app.database": MagicMock(),
             "app.utils": utils}
    return load("ira_manager", stubs)
