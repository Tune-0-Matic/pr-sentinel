"""Subprocess worker: runs the old and new version of a function on one input at a time.

Standalone on purpose (stdlib only, no package imports) so it can run as a plain script.
Protocol: one JSON request per stdin line; replies are stdout lines prefixed with MARK.
The parent enforces timeouts and restarts this process if it hangs or dies.
"""
import importlib.util
import io
import json
import os
import sys
import traceback
from decimal import Decimal

MARK = "@@pr-sentinel@@ "
EVAL_NAMES = {"Decimal": Decimal, "float": float, "list": list, "range": range, "__builtins__": {}}


def _repr(v, limit=300):
    try:
        text = repr(v)
    except Exception as e:  # a broken __repr__ must not take the worker down
        text = f"<unrepresentable {type(v).__name__}: {e}>"
    return text if len(text) <= limit else text[:limit] + "..."


def _text(exc):
    try:
        return str(exc)[:200]
    except Exception:
        return "<unprintable>"


def _same(a, b):
    if type(a).__name__ != type(b).__name__:
        return False
    try:
        if a == b or repr(a) == repr(b):
            return True
    except Exception:
        return False
    return hasattr(a, "__dict__") and hasattr(b, "__dict__") and vars(a) == vars(b)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and pickling look the module up by name
    spec.loader.exec_module(module)
    return module


def _trace(exc, new_path):
    frames = traceback.extract_tb(exc.__traceback__)
    start = next((i for i, f in enumerate(frames) if f.filename == new_path), 0)
    lines = [f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}\n    {(f.line or '').strip()}"
             for f in frames[start:]]
    line = next((f.lineno for f in reversed(frames) if f.filename == new_path), None)
    return "\n".join(lines + [f"{type(exc).__name__}: {exc}"]), line


def main():
    files = json.loads(sys.argv[1])  # {file_index: [old_path, new_path]}
    sys.path[:1] = json.loads(sys.argv[2])  # drop this script's folder, add the target repo's folders
    out, inp = sys.stdout, sys.stdin
    sys.stdout, sys.stdin = sys.stderr, io.StringIO()  # code under test must not touch the protocol

    def send(**msg):
        out.write(MARK + json.dumps(msg) + "\n")
        out.flush()

    modules = {}
    for fid, paths in files.items():
        for which, path in zip(("old", "new"), paths):
            try:
                modules[fid, which] = _load(f"{which}_{fid}", path)
            except BaseException as e:  # remembered and re-raised per call, so import errors are results
                modules[fid, which] = e
    send(ready=True)

    for raw in inp:
        req = json.loads(raw)
        fid = str(req["file"])
        new_path = files[fid][1]

        def call(which):
            mod = modules[fid, which]
            if isinstance(mod, BaseException):
                raise mod
            args = [eval(a, dict(EVAL_NAMES)) for a in req["args"]]
            kwargs = {k: eval(v, dict(EVAL_NAMES)) for k, v in req["kwargs"].items()}
            return getattr(mod, req["func"])(*args, **kwargs)

        try:
            old = ("ok", call("old"))
        except (Exception, SystemExit) as e:
            old = ("raised", e)
        send(phase="new")
        try:
            new = ("ok", call("new"))
        except (Exception, SystemExit) as e:
            new = ("raised", e)

        if old[0] == "ok" and new[0] == "ok":
            if _same(old[1], new[1]):
                send(result="pass")
            else:
                send(result="value_change",
                     detail=f"old returned {_repr(old[1])}; new returns {_repr(new[1])}")
        elif old[0] == "ok":
            trace, line = _trace(new[1], new_path)
            send(result="crash", error=type(new[1]).__name__, line=line, trace=trace,
                 detail=f"old returned {_repr(old[1])}; new raises {type(new[1]).__name__}: {_text(new[1])}")
        elif new[0] == "ok":
            send(result="now_accepts",
                 detail=f"old raised {type(old[1]).__name__}: {_text(old[1])}; new returns {_repr(new[1])}")
        else:
            send(result="pass")  # both versions reject the input


if __name__ == "__main__":
    main()
