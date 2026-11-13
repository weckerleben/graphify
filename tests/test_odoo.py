"""Odoo layer: model links, manifest depends, XML records, access CSV, incremental."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphify.extract import extract

MANIFEST_BASE = "{'name': 'Base', 'depends': ['base'], 'data': []}\n"
MANIFEST_EXT = "{'name': 'Ext', 'depends': ['mod_base', 'account'], 'data': []}\n"

BASE_PY = '''\
from odoo import api, fields, models


class Thing(models.Model):
    _name = "x.thing"
    _description = "Thing"

    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many(comodel_name="x.thing.line", inverse_name="thing_id")
    note = fields.Char()
    amount = fields.Float()
    total = fields.Float(compute="_compute_total")

    @api.depends("amount", "partner_id.name")
    def _compute_total(self):
        pass

    @api.constrains("amount")
    def _check_amount(self):
        pass

    @api.onchange("note")
    def _onchange_note(self):
        pass

    def action_confirm(self):
        pass
'''

EXT_PY = '''\
from odoo import models


class ThingExt(models.Model):
    _inherit = "x.thing"


class MoveExt(models.Model):
    _inherit = ["account.move", "mail.thread"]


class MoveWithMixin(models.Model):
    _name = "account.move"
    _inherit = ["account.move", "x.mixin"]


class Combo(models.Model):
    _name = "x.combo"
    _inherit = "x.thing"
    _inherits = {"res.partner": "partner_id"}
'''

VIEWS_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="thing_view_form" model="ir.ui.view">
        <field name="name">x.thing.form</field>
        <field name="model">x.thing</field>
        <field name="arch" type="xml">
            <form>
                <button name="action_confirm" type="object" string="Go"/>
                <field name="note"/>
                <field name="line_ids"><list><field name="qty"/></list></field>
            </form>
        </field>
    </record>
    <record id="thing_view_form_ext" model="ir.ui.view">
        <field name="name">x.thing.form.ext</field>
        <field name="model">x.thing</field>
        <field name="inherit_id" ref="mod_base.thing_view_form"/>
        <field name="arch" type="xml"><xpath expr="//field[@name='note']" position="after"><field name="amount"/></xpath></field>
    </record>
    <record id="thing_action" model="ir.actions.act_window">
        <field name="name">Things</field>
        <field name="res_model">x.thing</field>
        <field name="view_mode">list,form</field>
    </record>
    <menuitem id="thing_menu" name="Things" action="thing_action" parent="base.menu_custom"/>
    <record id="thing_rule" model="ir.rule">
        <field name="name">Thing rule</field>
        <field name="model_id" ref="model_x_thing"/>
        <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    </record>
    <record id="account_a" model="account.account"><field name="code">1</field></record>
    <record id="account_b" model="account.account"><field name="code">2</field></record>
</odoo>
"""

ACCESS_CSV = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "access_x_thing_user,x.thing user,model_x_thing,base.group_user,1,1,1,0\n"
)

V16_XML = """\
<?xml version="1.0"?>
<openerp>
  <data>
    <act_window id="legacy_action" name="Legacy" res_model="x.thing" view_mode="tree,form"/>
    <menuitem id="legacy_menu" name="Legacy" parent="base.menu_root" action="legacy_action"
              groups="base.group_user,base.group_system"/>
    <template id="legacy_tpl" inherit_id="web.layout"><t/></template>
  </data>
</openerp>
"""


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def corpus(tmp_path: Path):
    _write(tmp_path, "mod_base/__manifest__.py", MANIFEST_BASE)
    _write(tmp_path, "mod_base/__init__.py", "")
    _write(tmp_path, "mod_base/models/thing.py", BASE_PY)
    _write(tmp_path, "mod_ext/__manifest__.py", MANIFEST_EXT)
    _write(tmp_path, "mod_ext/models/ext.py", EXT_PY)
    _write(tmp_path, "mod_ext/views/thing_views.xml", VIEWS_XML)
    _write(tmp_path, "mod_ext/security/ir.model.access.csv", ACCESS_CSV)
    # an XML file from a non-Odoo project must stay untouched
    _write(tmp_path, "other/config.xml", "<config><a/></config>\n")
    paths = sorted(p for p in tmp_path.rglob("*") if p.suffix in {".py", ".xml", ".csv"})
    return extract(paths, root=tmp_path, cache_root=tmp_path, parallel=False)


def _by_id(result, node_id):
    return next((n for n in result["nodes"] if n["id"] == node_id), None)


def _edges(result, relation):
    return [e for e in result["edges"] if e["relation"] == relation]


def _label(result, node_id):
    return _by_id(result, node_id)["label"]


def _pairs(result, relation):
    return {(_label(result, e["source"]), _label(result, e["target"])) for e in _edges(result, relation)}


def test_model_hub_nodes_exist_for_defined_and_external_models(corpus):
    defined = _by_id(corpus, "odoo_model:x.thing")
    external = _by_id(corpus, "odoo_model:account.move")
    assert defined["label"] == "x.thing" and defined.get("defined_in_corpus") is True
    assert external["label"] == "account.move" and external.get("defined_in_corpus") is False


def test_inherit_string_links_extension_to_defining_class(corpus):
    assert ("ThingExt", "Thing") in _pairs(corpus, "extends")
    edge = next(e for e in _edges(corpus, "extends") if _label(corpus, e["source"]) == "ThingExt")
    assert edge["confidence"] == "INFERRED"


def test_inherit_list_and_external_model_use_placeholder(corpus):
    pairs = _pairs(corpus, "extends_model")
    assert ("MoveExt", "account.move") in pairs
    assert ("MoveExt", "mail.thread") in pairs
    assert all(e["confidence"] == "EXTRACTED" for e in _edges(corpus, "extends_model"))


def test_name_plus_inherit_and_inherits(corpus):
    assert ("Combo", "x.combo") in _pairs(corpus, "defines_model")
    assert ("x.combo", "x.thing") in _pairs(corpus, "inherits_model")
    assert ("x.combo", "res.partner") in _pairs(corpus, "delegates_to")


def test_name_repeated_in_inherit_is_an_extension_not_a_definition(corpus):
    # Odoo's common `_name = X` + `_inherit = [X, mixin]` pattern extends X
    assert ("MoveWithMixin", "account.move") in _pairs(corpus, "extends_model")
    assert ("MoveWithMixin", "account.move") not in _pairs(corpus, "defines_model")
    assert ("account.move", "x.mixin") in _pairs(corpus, "inherits_model")
    assert _by_id(corpus, "odoo_model:account.move")["defined_in_corpus"] is False


def test_relational_fields_link_models(corpus):
    pairs = _pairs(corpus, "relates_to")
    assert ("x.thing", "res.partner") in pairs
    assert ("x.thing", "x.thing.line") in pairs


def test_manifest_depends(corpus):
    pairs = _pairs(corpus, "depends_on")
    assert ("mod_ext", "mod_base") in pairs
    assert ("mod_ext", "account") in pairs
    assert _by_id(corpus, "odoo_module:account").get("defined_in_corpus") is False


def test_view_records_and_inheritance(corpus):
    labels = {n["label"] for n in corpus["nodes"]}
    assert "mod_ext.thing_view_form" in labels
    assert ("mod_ext.thing_view_form", "x.thing") in _pairs(corpus, "view_of")
    assert ("mod_ext.thing_view_form_ext", "mod_base.thing_view_form") in _pairs(corpus, "inherits_view")


def test_action_menu_model_chain(corpus):
    assert ("mod_ext.thing_action", "x.thing") in _pairs(corpus, "acts_on")
    assert ("mod_ext.thing_menu", "mod_ext.thing_action") in _pairs(corpus, "opens")
    assert ("mod_ext.thing_menu", "base.menu_custom") in _pairs(corpus, "child_of")


def test_rule_links_model_and_group(corpus):
    assert ("mod_ext.thing_rule", "x.thing") in _pairs(corpus, "rule_for")
    # model_x_thing is reverse-mapped from the model index, so it is a deduction
    assert next(e for e in _edges(corpus, "rule_for"))["confidence"] == "INFERRED"
    assert ("mod_ext.thing_rule", "base.group_user") in _pairs(corpus, "for_group")


def test_access_csv_links_model_and_group(corpus):
    assert ("mod_ext.access_x_thing_user", "x.thing") in _pairs(corpus, "grants_access_to")
    assert ("mod_ext.access_x_thing_user", "base.group_user") in _pairs(corpus, "for_group")


def test_plain_data_records_are_aggregated_not_minted(corpus):
    assert "mod_ext.account_a" not in {n["label"] for n in corpus["nodes"]}
    seeds = [e for e in _edges(corpus, "seeds_data") if _label(corpus, e["target"]) == "account.account"]
    assert len(seeds) == 1 and seeds[0]["context"] == "2 records"


def test_non_odoo_xml_is_not_extracted(corpus):
    assert not any(n["source_file"].endswith("other/config.xml") for n in corpus["nodes"])


def test_edges_reference_existing_nodes_and_carry_valid_confidence(corpus):
    ids = {n["id"] for n in corpus["nodes"]}
    odoo_relations = {"defines_model", "extends", "extends_model", "inherits_model", "delegates_to",
                      "relates_to", "depends_on", "view_of", "inherits_view", "acts_on", "opens",
                      "child_of", "rule_for", "for_group", "grants_access_to", "seeds_data"}
    odoo_edges = [e for e in corpus["edges"] if e["relation"] in odoo_relations]
    assert odoo_edges
    for e in odoo_edges:
        assert e["source"] in ids and e["target"] in ids, e
        assert e["confidence"] in {"EXTRACTED", "INFERRED", "AMBIGUOUS"}


def test_has_field_links_model_to_declared_fields(corpus):
    assert ("x.thing", "x.thing.note") in _pairs(corpus, "has_field")
    assert _by_id(corpus, "odoo_field:x.thing.note")["defined_in_corpus"] is True


def test_depends_links_computed_field_to_dependencies(corpus):
    pairs = _pairs(corpus, "depends_on_field")
    assert ("x.thing.total", "x.thing.amount") in pairs
    # partner_id is a Many2one to res.partner, so the path is followed
    assert ("x.thing.total", "res.partner.name") in pairs
    assert ("._compute_total()", "x.thing.total") in _pairs(corpus, "computes")


def test_constrains_and_onchange_link_method_to_field(corpus):
    assert ("._check_amount()", "x.thing.amount") in _pairs(corpus, "constrains")
    assert ("._onchange_note()", "x.thing.note") in _pairs(corpus, "onchange")


def test_view_arch_fields_buttons_and_subview_fields(corpus):
    shows = _pairs(corpus, "shows_field")
    assert ("mod_ext.thing_view_form", "x.thing.note") in shows
    # a field nested in a x2many field belongs to the comodel
    assert ("mod_ext.thing_view_form", "x.thing.line.qty") in shows
    assert ("mod_ext.thing_view_form", ".action_confirm()") in _pairs(corpus, "calls_method")


def test_xpath_and_inherited_view_fields(corpus):
    assert ("mod_ext.thing_view_form_ext", "x.thing.note") in _pairs(corpus, "patches_field")
    # fields added inside the xpath are shown by the inheriting view
    assert ("mod_ext.thing_view_form_ext", "x.thing.amount") in _pairs(corpus, "shows_field")


def test_v16_shorthand_tags_are_extracted(tmp_path):
    _write(tmp_path, "legacy/__manifest__.py", "{'name': 'L', 'depends': ['base']}\n")
    xml = _write(tmp_path, "legacy/views/v.xml", V16_XML)
    result = extract([xml], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert ("legacy.legacy_action", "x.thing") in _pairs(result, "acts_on")
    assert ("legacy.legacy_menu", "legacy.legacy_action") in _pairs(result, "opens")
    assert {("legacy.legacy_menu", "base.group_user"),
            ("legacy.legacy_menu", "base.group_system")} <= _pairs(result, "for_group")
    assert ("legacy.legacy_tpl", "web.layout") in _pairs(result, "inherits_view")


def test_xml_with_entity_declaration_is_rejected(tmp_path):
    _write(tmp_path, "m/__manifest__.py", "{'name': 'M', 'depends': []}\n")
    xml = _write(tmp_path, "m/data.xml",
                 '<?xml version="1.0"?><!DOCTYPE odoo [<!ENTITY a "aaaa">]>'
                 '<odoo><record id="r" model="ir.rule"/></odoo>')
    result = extract([xml], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert not any(n.get("odoo_kind") == "ir.rule" for n in result["nodes"])


def test_classification_is_marker_gated(tmp_path):
    from graphify.detect import FileType, classify_file

    odoo = _write(tmp_path, "m/a.xml", "<odoo/>")
    plain = _write(tmp_path, "m/b.xml", "<config/>")
    access = _write(tmp_path, "m/security/ir.model.access.csv", "id\n")
    other_csv = _write(tmp_path, "m/data.csv", "a,b\n")
    assert classify_file(odoo) == FileType.CODE
    assert classify_file(access) == FileType.CODE
    assert classify_file(plain) != FileType.CODE
    assert classify_file(other_csv) != FileType.CODE


def test_python_outside_an_odoo_module_is_not_stamped(tmp_path):
    py = _write(tmp_path, "lib/m.py", 'class A:\n    _inherit = "x.y"\n')
    result = extract([py], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert not any(n["id"].startswith("odoo_model:") for n in result["nodes"])
    assert not any(k.startswith("_odoo") for n in result["nodes"] for k in n)


def test_seed_xml_and_module_init_do_not_warn_about_missing_symbols(tmp_path, capfd):
    _write(tmp_path, "m/__manifest__.py", "{'name': 'M', 'depends': []}\n")
    init = _write(tmp_path, "m/__init__.py", "from . import models\n")
    seed = _write(tmp_path, "m/data/seed.xml",
                  '<odoo><record id="a" model="res.country.state"><field name="name">A</field></record></odoo>')
    extract([seed, init], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert "yielded no symbols" not in capfd.readouterr().err


def test_whool_pyproject_in_an_odoo_module_does_not_warn(tmp_path, capfd):
    _write(tmp_path, "m/__manifest__.py", "{'name': 'M', 'depends': []}\n")
    pyproject = _write(tmp_path, "m/pyproject.toml",
                       '[build-system]\nrequires = ["whool"]\nbuild-backend = "whool.buildapi"\n')
    extract([pyproject], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert "produced zero nodes" not in capfd.readouterr().err


def test_report_explains_odoo_hubs_among_god_nodes(corpus):
    from graphify.analyze import god_nodes, surprising_connections
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.report import _odoo_hub_note, generate

    G = build_from_json(corpus)
    communities = cluster(G)
    labels = {cid: f"Community {cid}" for cid in communities}
    detection = {"total_files": 8, "total_words": 100, "needs_graph": True, "warning": None}
    report = generate(G, communities, score_all(G, communities), labels, god_nodes(G, top_n=40),
                      surprising_connections(G), detection, {"input": 0, "output": 0}, "./p")
    assert "`x.thing` (Odoo model hub)" in report
    ext = {"id": "odoo_model:account.move", "label": "account.move"}
    assert _odoo_hub_note(G, ext) == " (Odoo model hub, not defined in this corpus)"


def test_record_overridden_in_another_module_links_to_the_canonical_record(tmp_path):
    _write(tmp_path, "a/__manifest__.py", "{'name': 'A', 'depends': []}\n")
    _write(tmp_path, "b/__manifest__.py", "{'name': 'B', 'depends': ['a']}\n")
    _write(tmp_path, "c/__manifest__.py", "{'name': 'C', 'depends': ['a']}\n")
    a = _write(tmp_path, "a/data/cron.xml", '<odoo><record id="job" model="ir.cron"/></odoo>')
    b = _write(tmp_path, "b/data/cron.xml", '<odoo><record id="a.job" model="ir.cron"/></odoo>')
    c = _write(tmp_path, "c/data/menu.xml", '<odoo><menuitem id="m" action="a.job"/></odoo>')
    result = extract([a, b, c], root=tmp_path, cache_root=tmp_path, parallel=False)
    ids = [n["id"] for n in result["nodes"] if n["label"] == "a.job"]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert ("a.job", "a.job") in _pairs(result, "overrides")
    canonical = next(e["target"] for e in _edges(result, "overrides"))
    assert _by_id(result, canonical)["source_file"] == "a/data/cron.xml"
    assert any(e["target"] == canonical for e in _edges(result, "opens"))


def test_prune_orphan_placeholders_keeps_referenced_and_defined_nodes():
    from graphify.extractors.odoo import prune_orphan_odoo_placeholders

    result = {
        "nodes": [
            {"id": "a", "label": "a", "odoo_kind": "external_ref"},
            {"id": "b", "label": "b", "odoo_kind": "model", "defined_in_corpus": False},
            {"id": "c", "label": "c", "odoo_kind": "model", "defined_in_corpus": True},
            {"id": "d", "label": "d", "odoo_kind": "model", "defined_in_corpus": False},
            {"id": "plain", "label": "plain"},
        ],
        "edges": [{"source": "plain", "target": "d", "relation": "extends_model"}],
    }
    assert {n["id"] for n in prune_orphan_odoo_placeholders(result)["nodes"]} == {"c", "d", "plain"}


def _graph_sets(root: Path):
    data = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: (n["label"], n.get("defined_in_corpus")) for n in data["nodes"]}
    edges = {(e["source"], e["target"], e["relation"]) for e in data["links"]}
    return nodes, edges


def test_incremental_rebuild_matches_a_full_rebuild(tmp_path, monkeypatch):
    from graphify.watch import _rebuild_code

    root = tmp_path / "inc"
    _write(root, "mod_base/__manifest__.py", MANIFEST_BASE)
    thing = _write(root, "mod_base/models/thing.py", BASE_PY)
    _write(root, "mod_ext/__manifest__.py", MANIFEST_EXT)
    ext = _write(root, "mod_ext/models/ext.py", EXT_PY)
    views = _write(root, "mod_ext/views/thing_views.xml", VIEWS_XML)
    _write(root, "mod_ext/security/ir.model.access.csv", ACCESS_CSV)
    monkeypatch.chdir(root)
    assert _rebuild_code(root)

    # edit one .py (new field) and one .xml (new view using it)
    thing.write_text(BASE_PY + "    extra = fields.Char()\n", encoding="utf-8")
    views.write_text(VIEWS_XML.replace(
        "</odoo>", '<record id="v2" model="ir.ui.view"><field name="model">res.partner</field>'
        '<field name="arch" type="xml"><form><field name="extra"/></form></field></record></odoo>'),
        encoding="utf-8")
    assert _rebuild_code(root, changed_paths=[thing, views])

    # delete the extension file: its placeholder hubs must not linger
    ext.unlink()
    assert _rebuild_code(root, changed_paths=[ext])

    full = tmp_path / "full"
    shutil.copytree(root, full, ignore=shutil.ignore_patterns("graphify-out"))
    # a separate process: the in-process stat index is bound to the first root
    done = subprocess.run([sys.executable, "-m", "graphify", "update", "."], cwd=full,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert _graph_sets(root) == _graph_sets(full)


def test_neutralize_sql_and_oca_readme_fragments_do_not_warn(tmp_path, capfd):
    _write(tmp_path, "m/__manifest__.py", "{'name': 'M', 'depends': []}\n")
    sql = _write(tmp_path, "m/data/neutralize.sql", "UPDATE ir_cron SET active = false;\n")
    md = _write(tmp_path, "m/readme/USAGE.md", "Open the menu and press the button.\n")
    extract([sql, md], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert "yielded no symbols" not in capfd.readouterr().err
