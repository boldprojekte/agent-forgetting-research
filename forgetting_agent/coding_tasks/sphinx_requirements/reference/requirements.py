"""Reference implementation for the authored Sphinx requirement task."""

from __future__ import annotations

import json
import re
from pathlib import Path

from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.domains import Domain
from sphinx.errors import ConfigError
from sphinx.roles import XRefRole
from sphinx.util import logging
from sphinx.util.docutils import SphinxDirective
from sphinx.util.nodes import make_refnode

logger = logging.getLogger(__name__)
ID_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*\Z")
COLUMNS = ("id", "title", "status", "owner", "tags", "depends")


def split_list(value):
    return sorted({p.strip() for p in (value or "").split(",") if p.strip()})


def warn(message, subtype, location):
    logger.warning(message, type="req", subtype=subtype, location=location)


class Overview(nodes.General, nodes.Element):
    pass


class Requirement(SphinxDirective):
    required_arguments = 0
    optional_arguments = 1
    final_argument_whitespace = True
    has_content = True
    option_spec = {
        name: directives.unchanged for name in ("title", "status", "owner", "tags", "depends")
    }

    def run(self):
        identity = self.arguments[0] if self.arguments else ""
        opts = self.options
        title = opts.get("title", "").strip()
        status = opts.get("status", "draft").strip()
        depends = split_list(opts.get("depends"))
        location = (self.env.docname, self.lineno)
        if not ID_PATTERN.fullmatch(identity) or any(not ID_PATTERN.fullmatch(d) for d in depends):
            warn(f"Invalid requirement ID in {identity!r}", "invalid_id", location)
            return []
        if not title:
            warn(f"Missing requirement title for {identity}", "invalid_title", location)
            return []
        if status not in self.config.req_statuses:
            warn(f"Invalid requirement status {status!r}", "invalid_status", location)
            return []
        record = dict(
            id=identity,
            title=title,
            status=status,
            owner=opts.get("owner", "").strip(),
            tags=split_list(opts.get("tags")),
            depends=depends,
            docname=self.env.docname,
            anchor="req-" + identity,
            line=self.lineno,
        )
        self.env.get_domain("req").data["items"].setdefault(self.env.docname, []).append(record)
        node = nodes.container(ids=[record["anchor"]], classes=["requirement"])
        self.set_source_info(node)
        node += nodes.paragraph("", "", nodes.strong("", f"{identity}: {title} ({status})"))
        self.state.nested_parse(self.content, self.content_offset, node)
        return [node]


class RequirementRef(XRefRole):
    def process_link(self, env, refnode, has_explicit_title, title, target):
        env.get_domain("req").data["refs"].setdefault(env.docname, set()).add(target)
        return title, target


class RequirementOverview(SphinxDirective):
    option_spec = {
        name: directives.unchanged for name in ("columns", "status", "tags", "owner", "sort")
    }

    def run(self):
        opts = self.options
        columns = [p.strip() for p in opts.get("columns", "id,title,status,owner").split(",")]
        statuses = split_list(opts.get("status"))
        sorting = opts.get("sort", "id").strip()
        if (
            not columns
            or any(c not in COLUMNS for c in columns)
            or len(set(columns)) != len(columns)
            or any(s not in self.config.req_statuses for s in statuses)
            or sorting not in ("id", "status")
        ):
            warn(
                "Invalid requirement overview option",
                "invalid_option",
                (self.env.docname, self.lineno),
            )
            return []
        node = Overview(
            columns=columns,
            statuses=statuses,
            tags=split_list(opts.get("tags")),
            owner=opts.get("owner"),
            sorting=sorting,
        )
        self.set_source_info(node)
        return [node]


class RequirementDomain(Domain):
    name = "req"
    label = "Requirements"
    directives = {"requirement": Requirement, "overview": RequirementOverview}
    roles = {"ref": RequirementRef()}
    initial_data = {"items": {}, "refs": {}}
    data_version = 1

    def canonical(self):
        result = {}
        for docname in sorted(self.data["items"]):
            for item in sorted(self.data["items"][docname], key=lambda r: r["line"]):
                result.setdefault(item["id"], item)
        return result

    def clear_doc(self, docname):
        self.data["items"].pop(docname, None)
        self.data["refs"].pop(docname, None)

    def merge_domaindata(self, docnames, otherdata):
        for docname in docnames:
            for field in ("items", "refs"):
                self.data[field].pop(docname, None)
                if docname in otherdata[field]:
                    self.data[field][docname] = otherdata[field][docname]

    def get_objects(self):
        for identity, item in sorted(self.canonical().items()):
            yield identity, item["title"], "requirement", item["docname"], item["anchor"], 1

    def resolve_xref(self, env, fromdocname, builder, typ, target, node, contnode):
        item = self.canonical().get(target)
        if item is None:
            warn(f"Unknown requirement {target}", "missing", node)
            return contnode
        label = contnode if node.get("refexplicit") else nodes.inline("", item["title"])
        return make_refnode(builder, fromdocname, item["docname"], item["anchor"], label)


def validate_config(app, config):
    values = config.req_statuses
    if (
        not isinstance(values, (list, tuple))
        or not values
        or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in values)
        or len(set(values)) != len(values)
    ):
        raise ConfigError("req_statuses must be a nonempty ordered list of unique trimmed strings")


def reread_dependents(app, env, added, changed, removed):
    # The registry is project-wide. Re-read on source changes to update all consumers,
    # including references that were previously unresolved and overview-only documents.
    return sorted(env.found_docs) if added or changed or removed else []


def validate_graph(app, env):
    domain = env.get_domain("req")
    canonical = domain.canonical()
    for items in domain.data["items"].values():
        for item in items:
            if item is not canonical[item["id"]]:
                warn(
                    f"Duplicate requirement {item['id']}",
                    "duplicate",
                    (item["docname"], item["line"]),
                )
    for item in canonical.values():
        for dep in item["depends"]:
            if dep not in canonical:
                warn(
                    f"Unknown requirement dependency {dep}",
                    "missing_dependency",
                    (item["docname"], item["line"]),
                )
    active, complete = set(), set()

    def visit(identity):
        if identity in active:
            item = canonical[identity]
            warn(
                f"Requirement dependency cycle at {identity}",
                "cycle",
                (item["docname"], item["line"]),
            )
            return
        if identity in complete or identity not in canonical:
            return
        active.add(identity)
        for dep in canonical[identity]["depends"]:
            visit(dep)
        active.remove(identity)
        complete.add(identity)

    for identity in sorted(canonical):
        visit(identity)


def render_overviews(app, doctree, docname):
    domain = app.env.get_domain("req")
    for node in list(doctree.findall(Overview)):
        items = [
            r
            for r in domain.canonical().values()
            if (not node["statuses"] or r["status"] in node["statuses"])
            and set(node["tags"]).issubset(r["tags"])
            and (node["owner"] is None or r["owner"] == node["owner"].strip())
        ]
        if node["sorting"] == "status":
            items.sort(key=lambda r: (app.config.req_statuses.index(r["status"]), r["id"]))
        else:
            items.sort(key=lambda r: r["id"])
        if not items:
            node.replace_self(nodes.paragraph("", "No matching requirements."))
            continue
        table = nodes.table(classes=["req-overview"])
        group = nodes.tgroup(cols=len(node["columns"]))
        table += group
        for _ in node["columns"]:
            group += nodes.colspec(colwidth=24)
        head = nodes.thead()
        row = nodes.row()
        for column in node["columns"]:
            row += nodes.entry("", nodes.paragraph("", column))
        head += row
        group += head
        body = nodes.tbody()
        for item in items:
            row = nodes.row()
            for column in node["columns"]:
                para = nodes.paragraph()
                if column == "id":
                    para += make_refnode(
                        app.builder,
                        docname,
                        item["docname"],
                        item["anchor"],
                        nodes.inline("", item["id"]),
                    )
                else:
                    value = item[column]
                    para += nodes.Text(", ".join(value) if isinstance(value, list) else value)
                row += nodes.entry("", para)
            body += row
        group += body
        node.replace_self(table)


def export_requirements(app, exception):
    if exception is not None:
        return
    domain = app.env.get_domain("req")
    items = []
    for identity, record in sorted(domain.canonical().items()):
        item = {k: v for k, v in record.items() if k != "line"}
        item["referenced_by"] = sorted(
            doc for doc, refs in domain.data["refs"].items() if identity in refs
        )
        items.append(item)
    payload = {"schema_version": 1, "requirements": items}
    Path(app.outdir, "requirements.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def setup(app):
    app.add_config_value("req_statuses", ["draft", "approved", "retired"], "env")
    app.add_domain(RequirementDomain)
    app.add_node(Overview)
    app.connect("config-inited", validate_config)
    app.connect("env-get-outdated", reread_dependents)
    app.connect("env-updated", validate_graph)
    app.connect("doctree-resolved", render_overviews)
    app.connect("build-finished", export_requirements)
    return {
        "version": "1.0",
        "env_version": 1,
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
