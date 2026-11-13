"""Odoo layer: model links, module deps, XML records and access rules.

Odoo wires most of its structure through strings (``_inherit = 'account.move'``,
``<field name="inherit_id" ref="x.y"/>``, ``model_account_move`` xml ids) that the
generic Python/XML extractors never see. This layer is opt-in by marker:
``__manifest__.py`` (Python), an ``<odoo>``/``<openerp>`` root (XML) and
``ir.model.access.csv``; anything else is untouched.

Per-file extractors only record raw facts on nodes (``_odoo_*`` attributes, so
they survive the AST cache); :func:`resolve_odoo` is the cross-file pass that
mints hub nodes (``odoo_model:``, ``odoo_module:``, ``odoo_field:``,
``odoo_xmlid:``) and edges. Direction: ``extract.py`` -> this module, never back.
"""
from __future__ import annotations

import ast
import csv
import re
from functools import lru_cache
from pathlib import Path
from xml.parsers import expat

from graphify.extractors.base import _make_id

MANIFEST_NAME = "__manifest__.py"
ACCESS_CSV_NAME = "ir.model.access.csv"
_XML_MAX_BYTES = 5 * 1024 * 1024
_SNIFF_BYTES = 4096
_ROOT_RE = re.compile(rb"<(?:odoo|openerp)[\s>/]")
_REL_FIELDS = {"Many2one", "One2many", "Many2many"}
_API_KINDS = {"depends", "constrains", "onchange"}
_REF_RE = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)")
_FIELD_PATH_RE = re.compile(r"field\[@name\s*=\s*['\"](\w+)['\"]\]")

# Records of these models become nodes; any other model's records (chart of
# accounts, states, demo data ...) are only counted per file, not minted.
_STRUCTURAL_MODELS = frozenset({
    "ir.ui.view", "ir.ui.menu", "ir.actions.act_window", "ir.actions.server",
    "ir.actions.report", "ir.actions.client", "ir.actions.act_url", "ir.rule",
    "res.groups", "ir.cron", "ir.model.access", "mail.template",
})
_GROUP_FIELDS = {"groups", "groups_id", "group_id", "implied_ids"}


# --------------------------------------------------------------------- gating

@lru_cache(maxsize=4096)
def _module_dir(directory: Path) -> Path | None:
    for candidate in (directory, *directory.parents):
        if (candidate / MANIFEST_NAME).is_file():
            return candidate
    return None


def odoo_module_dir(path: Path) -> Path | None:
    """Directory of the Odoo module that owns *path*, or None."""
    return _module_dir(path.parent)


def is_odoo_data_xml(path: Path) -> bool:
    """True for an Odoo data/view XML file (root ``<odoo>`` or ``<openerp>``)."""
    if path.suffix.lower() != ".xml":
        return False
    try:
        with open(path, "rb") as fh:
            return bool(_ROOT_RE.search(fh.read(_SNIFF_BYTES)))
    except OSError:
        return False


def is_odoo_access_csv(path: Path) -> bool:
    return path.name == ACCESS_CSV_NAME


def is_odoo_build_metadata(path: Path) -> bool:
    """``pyproject.toml`` next to a ``__manifest__.py`` that only declares the whool build backend."""
    if path.name != "pyproject.toml" or not (path.parent / MANIFEST_NAME).is_file():
        return False
    try:
        return "whool" in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def is_odoo_symbolless_by_design(path: Path) -> bool:
    """Files an Odoo module ships that legitimately define no symbols."""
    if odoo_module_dir(path) is None:
        return False
    return path.name == "neutralize.sql" or (path.suffix == ".md" and path.parent.name == "readme")


def extract_odoo_build_metadata(path: Path) -> dict:
    """Packaging metadata of an Odoo module: no package or dependency to model."""
    return {"nodes": [], "edges": [], "skipped": "odoo module build metadata"}


# --------------------------------------------------------------- python facts

def _str_list(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return None


def _comodel(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return _kw_str(call, "comodel_name")


def _kw_str(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _api_decorators(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[list]:
    found = []
    for dec in fn.decorator_list:
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in _API_KINDS and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "api"):
            args = [a.value for a in dec.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if args:
                found.append([fn.name, fn.lineno, dec.func.attr, args])
    return found


def _class_facts(cls: ast.ClassDef) -> dict | None:
    name: str | None = None
    inherit: list[str] = []
    inherits: dict[str, str] = {}
    fields: list[list] = []
    methods: list[list] = []
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.extend(_api_decorators(stmt))
            continue
        target = value = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if target.id == "_name":
            names = _str_list(value)
            name = names[0] if names else None
        elif target.id == "_inherit":
            inherit = _str_list(value) or []
        elif target.id == "_inherits" and isinstance(value, ast.Dict):
            inherits = {k.value: v.value for k, v in zip(value.keys, value.values)
                        if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                        and isinstance(k.value, str) and isinstance(v.value, str)}
        elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
              and isinstance(value.func.value, ast.Name) and value.func.value.id == "fields"):
            comodel = _comodel(value) if value.func.attr in _REL_FIELDS else None
            fields.append([target.id, comodel, _kw_str(value, "compute")])
    if name is None and not inherit:
        return None
    return {"name": name, "inherit": inherit, "inherits": inherits, "fields": fields, "methods": methods}


def _literal_manifest(source: str) -> dict:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in tree.body:
        value = node.value if isinstance(node, (ast.Expr, ast.Assign)) else None
        if isinstance(value, ast.Dict):
            try:
                data = ast.literal_eval(value)
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def annotate_python_result(path: Path, result: dict) -> dict:
    """Stamp Odoo facts onto the nodes of an ``extract_python`` result."""
    if "error" in result:
        return result
    is_manifest = path.name == MANIFEST_NAME
    if not is_manifest and odoo_module_dir(path) is None:
        return result
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    if is_manifest:
        module = path.parent.name
        depends = _literal_manifest(source).get("depends") or []
        result["nodes"].append({
            "id": f"odoo_module:{module}", "label": module, "file_type": "code",
            "source_file": str(path), "source_location": "L1",
            "odoo_kind": "module", "defined_in_corpus": True,
            "_odoo_depends": [d for d in depends if isinstance(d, str)],
        })
        return result
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return result
    facts = {n.lineno: f for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and (f := _class_facts(n))}
    if not facts:
        return result
    for node in result["nodes"]:
        loc = str(node.get("source_location") or "")
        if loc.startswith("L") and loc[1:].isdigit() and int(loc[1:]) in facts:
            node["_odoo_model"] = facts[int(loc[1:])]
    return result


# ------------------------------------------------------------------ xml/csv

def _qualify(xmlid: str, module: str) -> str:
    return xmlid if "." in xmlid else f"{module}.{xmlid}"


class _El:
    """Minimal element: expat gives line numbers, ElementTree's C parser does not."""
    __slots__ = ("tag", "attrib", "text", "children", "line")

    def __init__(self, tag: str, attrib: dict, line: int):
        self.tag, self.attrib, self.text, self.children, self.line = tag, attrib, "", [], line

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.attrib.get(key, default)

    def findall(self, tag: str) -> list["_El"]:
        return [c for c in self.children if c.tag == tag]

    def iter(self):
        yield self
        for child in self.children:
            yield from child.iter()


def _parse_xml(path: Path) -> _El | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # Entity declarations are never used in Odoo data; refusing them closes the
    # expansion-bomb class of attacks for the stdlib parser.
    if len(raw) > _XML_MAX_BYTES or b"<!ENTITY" in raw:
        return None
    parser = expat.ParserCreate()
    stack: list[_El] = []
    top: list[_El] = []

    def start(tag, attrs):
        el = _El(tag, dict(attrs), parser.CurrentLineNumber)
        (stack[-1].children if stack else top).append(el)
        stack.append(el)

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda _tag: stack.pop()
    parser.CharacterDataHandler = lambda data: stack and setattr(stack[-1], "text", stack[-1].text + data)
    try:
        parser.Parse(raw, True)
    except expat.ExpatError:
        return None
    return top[0] if top else None


class _FileBuilder:
    def __init__(self, path: Path, module: str):
        self.path, self.module = path, module
        self.src = str(path)
        self.file_nid = _make_id(str(path))
        self.nodes: list[dict] = [{
            "id": self.file_nid, "label": path.name, "file_type": "code",
            "source_file": self.src, "source_location": "L1", "_odoo_refs": [],
        }]
        self.edges: list[dict] = []
        self.seeds: dict[str, int] = {}
        self._seen: set[str] = set()

    def record(self, rid: str, kind: str, line: int) -> dict | None:
        xmlid = _qualify(rid, self.module)
        nid = _make_id(str(self.path), xmlid)
        if nid in self._seen:
            return None
        self._seen.add(nid)
        node = {"id": nid, "label": xmlid, "file_type": "code", "source_file": self.src,
                "source_location": f"L{line}", "odoo_kind": kind, "_odoo_refs": [],
                "_odoo_xmlid": xmlid, "_odoo_module": self.module}
        self.nodes.append(node)
        self.edges.append({"source": self.file_nid, "target": nid, "relation": "contains",
                           "confidence": "EXTRACTED", "source_file": self.src,
                           "source_location": f"L{line}", "weight": 1.0})
        return node

    def ref(self, node: dict, relation: str, kind: str, target: str, line: int,
            context: str | None = None) -> None:
        if target:
            node["_odoo_refs"].append([relation, kind, target, line, context])

    def finish(self) -> dict:
        for model, count in sorted(self.seeds.items()):
            self.ref(self.nodes[0], "seeds_data", "model", model, 1, f"{count} records")
        return {"nodes": self.nodes, "edges": self.edges, "data_only": True}


def _split_refs(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def _walk_arch(fb: _FileBuilder, node: dict, el: _El, in_field: str | None) -> None:
    """Fields, xpath targets and object buttons inside a view ``arch``."""
    for child in el.children:
        tag, name = child.tag, child.get("name")
        if tag == "field" and name:
            path = f"{in_field}.{name}" if in_field else name
            if child.get("position") and not in_field:
                fb.ref(node, "patches_field", "field", path, child.line, child.get("position"))
            else:
                fb.ref(node, "shows_field", "field", path, child.line)
            _walk_arch(fb, node, child, path)
        elif tag == "xpath":
            match = _FIELD_PATH_RE.search(child.get("expr") or "")
            if match:
                fb.ref(node, "patches_field", "field", match.group(1), child.line,
                       f"{child.get('position') or 'inside'} {child.get('expr')}")
            _walk_arch(fb, node, child, None)
        else:
            if tag == "button" and name and child.get("type") == "object":
                fb.ref(node, "calls_method", "method", name, child.line)
            _walk_arch(fb, node, child, in_field)


def _record_refs(fb: _FileBuilder, node: dict, model: str, el: _El, line: int) -> None:
    for field in el.findall("field"):
        fname = field.get("name") or ""
        text = (field.text or "").strip()
        ref = field.get("ref")
        eval_refs = _REF_RE.findall(field.get("eval") or "")
        ids = ([ref] if ref else []) + eval_refs
        if model == "ir.ui.view" and fname == "arch":
            _walk_arch(fb, node, field, None)
        elif model == "ir.ui.view" and fname == "model" and text:
            node["_odoo_view_model"] = text
            fb.ref(node, "view_of", "model", text, line)
        elif model == "ir.ui.view" and fname == "inherit_id":
            for i in ids:
                fb.ref(node, "inherits_view", "xmlid", _qualify(i, fb.module), line)
        elif model == "ir.actions.act_window" and fname == "res_model" and text:
            fb.ref(node, "acts_on", "model", text, line)
        elif fname == "model_id" and ref:
            rel = "rule_for" if model == "ir.rule" else "acts_on"
            fb.ref(node, rel, "model_ref", ref, line)
        elif fname in _GROUP_FIELDS:
            rel = "implies" if fname == "implied_ids" else "for_group"
            for i in ids:
                fb.ref(node, rel, "xmlid", _qualify(i, fb.module), line)
        elif fname in {"view_id", "search_view_id"}:
            for i in ids:
                fb.ref(node, "uses_view", "xmlid", _qualify(i, fb.module), line)
        elif fname in {"parent_id", "action"} and model == "ir.ui.menu":
            rel = "child_of" if fname == "parent_id" else "opens"
            for i in ids:
                fb.ref(node, rel, "xmlid", _qualify(i, fb.module), line)
        else:
            for i in ids:
                fb.ref(node, "refers_to", "xmlid", _qualify(i, fb.module), line, fname)


def extract_odoo_xml(path: Path) -> dict:
    """Records, menus, actions and templates of an Odoo data/view XML file."""
    module_dir = odoo_module_dir(path)
    if module_dir is None or not is_odoo_data_xml(path):
        return {"nodes": [], "edges": [], "skipped": "not an Odoo data file"}
    root = _parse_xml(path)
    if root is None:
        return {"nodes": [], "edges": [], "error": "odoo xml unreadable or rejected"}
    fb = _FileBuilder(path, module_dir.name)
    for el in root.iter():
        line = el.line
        rid, tag = el.get("id"), el.tag
        if tag == "record":
            model = el.get("model") or ""
            if model not in _STRUCTURAL_MODELS or not rid:
                if model:
                    fb.seeds[model] = fb.seeds.get(model, 0) + 1
                continue
            node = fb.record(rid, model, line)
            if node is not None:
                _record_refs(fb, node, model, el, line)
        elif tag == "menuitem" and rid:
            node = fb.record(rid, "ir.ui.menu", line)
            if node is not None:
                if el.get("action"):
                    fb.ref(node, "opens", "xmlid", _qualify(el.get("action", ""), fb.module), line)
                if el.get("parent"):
                    fb.ref(node, "child_of", "xmlid", _qualify(el.get("parent", ""), fb.module), line)
                for g in _split_refs(el.get("groups") or ""):
                    fb.ref(node, "for_group", "xmlid", _qualify(g.lstrip("-"), fb.module), line)
        elif tag == "act_window" and rid:
            node = fb.record(rid, "ir.actions.act_window", line)
            if node is not None and el.get("res_model"):
                fb.ref(node, "acts_on", "model", el.get("res_model", ""), line)
        elif tag == "template" and rid:
            node = fb.record(rid, "ir.ui.view", line)
            if node is not None:
                _walk_arch(fb, node, el, None)
                if el.get("inherit_id"):
                    fb.ref(node, "inherits_view", "xmlid", _qualify(el.get("inherit_id", ""), fb.module), line)
    return fb.finish()


def extract_odoo_access_csv(path: Path) -> dict:
    """One node per ``ir.model.access.csv`` row, linked to its model and group."""
    module_dir = odoo_module_dir(path)
    if module_dir is None:
        return {"nodes": [], "edges": [], "skipped": "not in an Odoo module"}
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}
    fb = _FileBuilder(path, module_dir.name)
    for index, row in enumerate(rows):
        rid = (row.get("id") or "").strip()
        if not rid:
            continue
        node = fb.record(rid, "ir.model.access", index + 2)
        if node is None:
            continue
        model = (row.get("model_id:id") or "").strip()
        group = (row.get("group_id:id") or "").strip()
        fb.ref(node, "grants_access_to", "model_ref", model, index + 2)
        if group:
            fb.ref(node, "for_group", "xmlid", _qualify(group, fb.module), index + 2)
    return fb.finish()


# ---------------------------------------------------------------- resolution

def _defines(facts: dict) -> bool:
    """``_name = X`` with X absent from ``_inherit``; otherwise the class extends X."""
    return bool(facts["name"]) and facts["name"] not in facts["inherit"]


def _owner(facts: dict) -> str | None:
    return facts["name"] or (facts["inherit"][0] if len(facts["inherit"]) == 1 else None)


def resolve_odoo(per_file: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    """Mint hub nodes and cross-file edges from the raw ``_odoo_*`` facts."""
    by_id = {n["id"]: n for n in nodes}
    seen_edges = {(e["source"], e["target"], e["relation"]) for e in edges}

    def add_node(nid: str, label: str, source: str, loc: str | None, **extra) -> dict:
        node = by_id.get(nid)
        if node is None:
            node = {"id": nid, "label": label, "file_type": "code",
                    "source_file": source, "source_location": loc, **extra}
            nodes.append(node)
            by_id[nid] = node
        return node

    def add_edge(src: str, tgt: str, relation: str, conf: str, source: str,
                 loc: str | None, context: str | None = None) -> None:
        key = (src, tgt, relation)
        if src == tgt or key in seen_edges:
            return
        seen_edges.add(key)
        edge = {"source": src, "target": tgt, "relation": relation, "confidence": conf,
                "confidence_score": 1.0 if conf == "EXTRACTED" else 0.85,
                "source_file": source, "source_location": loc, "weight": 1.0}
        if context:
            edge["context"] = context
        edges.append(edge)

    classes = [n for n in nodes if n.get("_odoo_model")]
    # model name -> first defining class node (only an explicit ``_name``)
    defining: dict[str, dict] = {}
    for node in sorted(classes, key=lambda n: n["source_file"]):
        if _defines(node["_odoo_model"]):
            defining.setdefault(node["_odoo_model"]["name"], node)
    # (model, field) -> comodel, across every class that declares it
    comodels = {(_owner(c["_odoo_model"]), f[0]): f[1]
                for c in classes for f in c["_odoo_model"]["fields"] if f[1] and _owner(c["_odoo_model"])}
    method_at = {(n["source_file"], n.get("source_location")): n
                 for n in nodes if str(n.get("label", "")).startswith(".") and str(n["label"]).endswith("()")}
    class_methods: dict[str, dict[str, str]] = {}
    for e in edges:
        if e["relation"] == "method" and e["source"] in by_id and e["target"] in by_id:
            class_methods.setdefault(e["source"], {})[str(by_id[e["target"]]["label"]).strip(".()")] = e["target"]

    def model_hub(name: str) -> str:
        nid = f"odoo_model:{name}"
        cls = defining.get(name)
        hub = add_node(nid, name, cls["source_file"] if cls else "", cls and cls.get("source_location"),
                       odoo_kind="model", defined_in_corpus=bool(cls))
        if cls and not hub.get("defined_in_corpus"):
            hub.update(defined_in_corpus=True, source_file=cls["source_file"])
        return nid

    def field_node(model: str, fname: str, cls: dict | None = None) -> str:
        nid = f"odoo_field:{model}.{fname}"
        node = add_node(nid, f"{model}.{fname}", cls["source_file"] if cls else "",
                        cls and cls.get("source_location"), odoo_kind="field", defined_in_corpus=bool(cls))
        if cls and not node.get("defined_in_corpus"):
            node.update(defined_in_corpus=True, source_file=cls["source_file"])
        return nid

    def resolve_path(model: str, path: str) -> tuple[str, str, bool]:
        """Follow ``a.b.c`` through relational fields; fall back to the first segment."""
        parts, cur = path.split("."), model
        for seg in parts[:-1]:
            nxt = comodels.get((cur, seg))
            if not nxt:
                return model, parts[0], len(parts) == 1
            cur = nxt
        return cur, parts[-1], len(parts) == 1

    # python: _name / _inherit / _inherits / fields / api decorators
    for cls in classes:
        facts, src, loc = cls["_odoo_model"], cls["source_file"], cls.get("source_location")
        owner = _owner(facts)
        if _defines(facts):
            add_edge(cls["id"], model_hub(facts["name"]), "defines_model", "EXTRACTED", src, loc)
        for parent in facts["inherit"]:
            hub = model_hub(parent)
            if facts["name"] and parent != facts["name"]:
                add_edge(model_hub(facts["name"]), hub, "inherits_model", "EXTRACTED", src, loc)
            else:
                add_edge(cls["id"], hub, "extends_model", "EXTRACTED", src, loc)
            target = defining.get(parent)
            if target is not None and target["id"] != cls["id"]:
                add_edge(cls["id"], target["id"], "extends", "INFERRED", src, loc)
        if not owner:
            continue
        for parent in facts["inherits"]:
            add_edge(model_hub(owner), model_hub(parent), "delegates_to", "EXTRACTED", src, loc)
        for fname, comodel, _compute in facts["fields"]:
            add_edge(model_hub(owner), field_node(owner, fname, cls), "has_field", "EXTRACTED", src, loc)
            if comodel:
                add_edge(model_hub(owner), model_hub(comodel), "relates_to", "EXTRACTED", src, loc, fname)
        for mname, line, kind, args in facts["methods"]:
            meth = method_at.get((src, f"L{line}"))
            computed = [f[0] for f in facts["fields"] if f[2] == mname] if kind == "depends" else []
            origins = [field_node(owner, f) for f in computed] or ([meth["id"]] if meth else [])
            for fname in computed:
                if meth:
                    add_edge(meth["id"], field_node(owner, fname), "computes", "EXTRACTED", src, f"L{line}")
            for arg in args:
                tm, tf, exact = resolve_path(owner, arg)
                conf = "EXTRACTED" if exact else "INFERRED"
                target = field_node(tm, tf)
                if kind == "depends":
                    for origin in origins:
                        add_edge(origin, target, "depends_on_field", conf, src, f"L{line}")
                elif meth:
                    add_edge(meth["id"], target, kind, conf, src, f"L{line}")

    # manifests: module depends
    for mod in [n for n in nodes if n.get("_odoo_depends") is not None]:
        for dep in mod["_odoo_depends"]:
            dep_id = f"odoo_module:{dep}"
            add_node(dep_id, dep, "", None, odoo_kind="module", defined_in_corpus=False)
            add_edge(mod["id"], dep_id, "depends_on", "EXTRACTED", mod["source_file"], mod.get("source_location"))
        mod["defined_in_corpus"] = True

    # xml ids: one canonical record per id (the one in the id's own module);
    # a record redefined elsewhere (an override) links to it instead of colliding.
    by_xmlid: dict[str, list[dict]] = {}
    for n in nodes:
        if n.get("_odoo_xmlid"):
            by_xmlid.setdefault(n["_odoo_xmlid"], []).append(n)
    canonical: dict[str, dict] = {}
    for xmlid, group in by_xmlid.items():
        group.sort(key=lambda n: n["source_file"])
        canon = next((n for n in group if n["_odoo_module"] == xmlid.split(".")[0]), group[0])
        canonical[xmlid] = canon
        for other in group:
            if other is not canon:
                add_edge(other["id"], canon["id"], "overrides", "EXTRACTED",
                         other["source_file"], other.get("source_location"))

    def xmlid_id(xmlid: str) -> str:
        if xmlid in canonical:
            return canonical[xmlid]["id"]
        return add_node(f"odoo_xmlid:{xmlid}", xmlid, "", None, odoo_kind="external_ref")["id"]

    # views: model of each view, following inherit_id when the record omits it
    inherits = {}
    for n in nodes:
        for rel, _k, target, *_ in n.get("_odoo_refs", ()):
            if rel == "inherits_view" and target in canonical:
                inherits[n["id"]] = canonical[target]["id"]

    def view_model(nid: str) -> str | None:
        for _ in range(10):
            node = by_id.get(nid)
            if node is None:
                return None
            if node.get("_odoo_view_model"):
                return node["_odoo_view_model"]
            nid = inherits.get(nid, "")
        return None

    # xml/csv: refs
    known_models = {n["label"] for n in nodes if n.get("odoo_kind") == "model"}
    for c in classes:
        f = c["_odoo_model"]
        known_models.update([x for x in [f["name"], *f["inherit"], *f["inherits"]] if x])
    known_models.update(comodels.values())
    reverse: dict[str, list[str]] = {}
    for name in sorted(known_models):
        reverse.setdefault(name.replace(".", "_"), []).append(name)
    for node in [n for n in nodes if n.get("_odoo_refs")]:
        src, nid = node["source_file"], node["id"]
        for relation, kind, target, line, context in node["_odoo_refs"]:
            loc = f"L{line}"
            if kind == "model":
                add_edge(nid, model_hub(target), relation, "EXTRACTED", src, loc, context)
            elif kind == "field":
                model = view_model(nid)
                if model:
                    tm, tf, exact = resolve_path(model, target)
                    add_edge(nid, field_node(tm, tf), relation, "EXTRACTED" if exact else "INFERRED",
                             src, loc, context)
            elif kind == "method":
                model = view_model(nid)
                for cls in classes:
                    if _owner(cls["_odoo_model"]) == model and target in class_methods.get(cls["id"], {}):
                        add_edge(nid, class_methods[cls["id"]][target], relation, "INFERRED", src, loc, context)
            elif kind == "model_ref":
                tail = target.rsplit(".", 1)[-1]
                matches = reverse.get(tail[len("model_"):], []) if tail.startswith("model_") else []
                if len(matches) == 1:
                    add_edge(nid, model_hub(matches[0]), relation, "INFERRED", src, loc, context)
                else:
                    add_edge(nid, xmlid_id(target), relation, "EXTRACTED", src, loc, context)
            else:
                add_edge(nid, xmlid_id(target), relation, "EXTRACTED", src, loc, context)


def prune_orphan_odoo_placeholders(result: dict) -> dict:
    """Drop placeholder hubs that no edge references any more.

    A full build never mints an unreferenced placeholder, but an incremental
    rebuild keeps the ones whose only referrer was just edited or deleted.
    """
    key = "links" if "links" in result else "edges"
    used = {e.get("source") for e in result.get(key, [])} | {e.get("target") for e in result.get(key, [])}
    keep = [n for n in result["nodes"]
            if n["id"] in used or not (n.get("odoo_kind") == "external_ref"
                                       or (n.get("odoo_kind") in {"model", "module", "field"}
                                           and n.get("defined_in_corpus") is False))]
    return {**result, "nodes": keep}
