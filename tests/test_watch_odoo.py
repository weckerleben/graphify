"""`graphify watch` must react to Odoo module XML/CSV edits, and only to those."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.watch import _batch_needs_llm_flag, _batch_triggers_rebuild, _is_watched_path

pytest.importorskip("watchdog")

MANIFEST = "{'name': 'm', 'depends': ['base']}\n"
VIEWS = "<odoo>\n  <record id=\"v\" model=\"ir.ui.view\"/>\n</odoo>\n"
ACCESS = "id,name,model_id:id,group_id:id,perm_read\na,a,model_x,base.group_user,1\n"


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def tree(tmp_path: Path) -> dict[str, Path]:
    _write(tmp_path, "mod/__manifest__.py", MANIFEST)
    return {
        "xml": _write(tmp_path, "mod/views/v.xml", VIEWS),
        "csv": _write(tmp_path, "mod/security/ir.model.access.csv", ACCESS),
        "other_xml": _write(tmp_path, "plain/config.xml", "<config/>\n"),
        "other_csv": _write(tmp_path, "plain/data.csv", "a,b\n1,2\n"),
        "py": _write(tmp_path, "plain/a.py", "x = 1\n"),
    }


def test_module_xml_and_csv_are_watched(tree):
    assert _is_watched_path(tree["xml"])
    assert _is_watched_path(tree["csv"])


def test_xml_and_csv_outside_a_module_stay_ignored(tree):
    assert not _is_watched_path(tree["other_xml"])
    assert not _is_watched_path(tree["other_csv"])
    assert _is_watched_path(tree["py"])


def test_module_xml_and_csv_trigger_a_rebuild_not_the_llm_flag(tree):
    batch = [tree["xml"], tree["csv"]]
    assert _batch_triggers_rebuild(batch)
    assert not _batch_needs_llm_flag(batch)


def test_deleted_module_xml_still_triggers_a_rebuild(tree):
    tree["xml"].unlink()
    assert _batch_triggers_rebuild([tree["xml"]])


def test_unrelated_xml_does_not_trigger_a_rebuild(tree):
    assert not _batch_triggers_rebuild([tree["other_xml"]])
