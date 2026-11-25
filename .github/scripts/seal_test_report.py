#!/usr/bin/env python3
"""Summarize Gradle JUnit XML results for one CI leg.

Prints failed tests, every skipped test grouped by class with the reason found
in the test source (the Gradle XML carries no skip message), then per-module
totals and a final TEST TOTAL line, so the summary is the last thing in the log.
"""
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict

LEG = sys.argv[1] if len(sys.argv) > 1 else "all"

SKIP_ANNOTATION = re.compile(
    r"@(Disabled\w*|Enabled\w*|Ignore|IfProfileValue|ProfileValueSourceConfiguration|"
    r"DisabledIf\w*|EnabledIf\w*|EnabledForTestGroups|Assume\w*)\b"
)
ASSUME_CALL = re.compile(r"\b(assume\w*|Assume\.\w+|Assumptions\.\w+)\s*\(")
SOURCE_ROOTS = ("src/test/java", "src/test/kotlin", "src/testFixtures/java", "src/testFixtures/kotlin")

_file_cache = {}


def find_source(module, fqcn):
    outer = fqcn.split("$")[0]
    rel = outer.replace(".", "/")
    key = (module, rel)
    if key in _file_cache:
        return _file_cache[key]
    candidates = []
    for root in SOURCE_ROOTS:
        for ext in (".java", ".kt"):
            candidates.append(os.path.join(module, root, rel + ext))
    for root in SOURCE_ROOTS:
        for ext in (".java", ".kt"):
            candidates.extend(glob.glob(os.path.join("*", root, rel + ext)))
    path = next((c for c in candidates if os.path.isfile(c)), None)
    lines = None
    if path:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    _file_cache[key] = (path, lines)
    return path, lines


def is_comment(line):
    s = line.strip()
    return s.startswith("//") or s.startswith("*") or s.startswith("/*")


def annotations_above(lines, idx):
    """Collect skip-related annotation lines directly above declaration line idx."""
    found = []
    j = idx - 1
    while j >= 0:
        s = lines[j].strip()
        if not s or is_comment(lines[j]):
            j -= 1
            continue
        if s.startswith("@") or s.startswith(")") or s.endswith(",") or s.startswith('"'):
            if SKIP_ANNOTATION.search(s):
                found.append(s)
            j -= 1
            continue
        break
    # same-line annotations (Kotlin / compact Java)
    if SKIP_ANNOTATION.search(lines[idx]):
        found.append(lines[idx].strip())
    return list(reversed(found))


def method_body(lines, idx):
    depth = 0
    started = False
    out = []
    for k in range(idx, min(len(lines), idx + 400)):
        line = lines[k]
        out.append(line)
        depth += line.count("{") - line.count("}")
        if "{" in line:
            started = True
        if started and depth <= 0:
            break
    return out


def method_reason(lines, method):
    if not method:
        return None
    decl = re.compile(r"(\bvoid|\bfun)\s+`?" + re.escape(method) + r"`?\s*\(")
    for i, line in enumerate(lines):
        if is_comment(line) or not decl.search(line):
            continue
        anns = annotations_above(lines, i)
        if anns:
            return " ".join(anns)
        assumes = [b.strip() for b in method_body(lines, i) if ASSUME_CALL.search(b) and not is_comment(b)]
        if assumes:
            return "assumption: " + " | ".join(assumes[:3])
    return None


def class_reason(lines, simple_name):
    decl = re.compile(r"\b(class|interface|object)\s+" + re.escape(simple_name) + r"\b")
    for i, line in enumerate(lines):
        if is_comment(line) or not decl.search(line):
            continue
        anns = annotations_above(lines, i)
        if anns:
            return " ".join(anns)
    return None


def file_assumes(lines):
    calls = []
    for line in lines:
        if is_comment(line):
            continue
        if ASSUME_CALL.search(line):
            s = line.strip()
            if s not in calls:
                calls.append(s)
    if calls:
        return "assumption in class: " + " | ".join(calls[:3])
    return None


def superclass(lines, simple_name):
    decl = re.compile(r"\b(class)\s+" + re.escape(simple_name) + r"\b[^{]*?\bextends\s+([\w.]+)")
    text = "\n".join(l for l in lines if not is_comment(l))
    m = decl.search(text)
    if not m:
        m = re.search(r"\bclass\s+" + re.escape(simple_name) + r"\b[^{]*?:\s*([\w.]+)\s*\(", text)
        if not m:
            return None
        return m.group(1)
    return m.group(2)


def resolve_fqcn(lines, fqcn, simple):
    pkg = fqcn.split("$")[0].rsplit(".", 1)[0]
    for line in lines:
        m = re.match(r"\s*import\s+([\w.]+\." + re.escape(simple.split(".")[-1]) + r")\s*;?\s*$", line)
        if m:
            return m.group(1)
    if "." in simple:
        return simple
    return pkg + "." + simple


def reason_for(module, fqcn, method, depth=0):
    path, lines = find_source(module, fqcn)
    if not lines:
        return None
    simple_names = fqcn.split(".")[-1].split("$")
    r = method_reason(lines, method)
    if r:
        return r
    for name in reversed(simple_names):
        r = class_reason(lines, name)
        if r:
            return r
    r = file_assumes(lines)
    if r:
        return r
    if depth < 4:
        sup = superclass(lines, simple_names[-1])
        if sup:
            sup_fqcn = resolve_fqcn(lines, fqcn, sup)
            r = reason_for(module, sup_fqcn, method, depth + 1)
            if r:
                return r + " (inherited from " + sup_fqcn.split(".")[-1] + ")"
    return None


def method_name(testcase_name):
    m = re.match(r"\s*`?([A-Za-z_][\w ]*?)`?\s*\(", testcase_name)
    if m:
        return m.group(1)
    # JUnit 4 parameterized: "method[param]"; plain: "method"
    m = re.match(r"\s*([A-Za-z_]\w*)\s*(\[.*)?$", testcase_name)
    if m:
        return m.group(1)
    return None


def main():
    files = sorted(glob.glob("*/build/test-results/**/TEST-*.xml", recursive=True))
    per_module = OrderedDict()
    failures = []
    skips = defaultdict(list)
    for f in files:
        module = f.split(os.sep)[0]
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError as exc:
            print("WARN unparsable %s: %s" % (f, exc))
            continue
        stats = per_module.setdefault(module, [0, 0, 0, 0])
        for tc in root.iter("testcase"):
            cls = tc.get("classname", "")
            name = tc.get("name", "")
            stats[0] += 1
            if tc.find("failure") is not None or tc.find("error") is not None:
                stats[2] += 1
                node = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                failures.append((module, cls, name, (node.get("message") or "")[:300]))
            elif tc.find("skipped") is not None:
                stats[3] += 1
                skips[(module, cls)].append(name)
            else:
                stats[1] += 1

    for module, cls, name, msg in failures:
        print("FAILED TEST [%s] %s.%s: %s" % (module, cls, name, msg))

    print("==== SKIPPED TESTS [%s] (grouped by class, reason from test source) ====" % LEG)
    for (module, cls), names in sorted(skips.items()):
        reasons = OrderedDict()
        for n in names:
            r = reason_for(module, cls, method_name(n)) or (
                "skipped by JUnit at runtime (no static condition in source; dynamic assumption/condition)"
            )
            reasons.setdefault(r, []).append(n)
        for r, ns in reasons.items():
            shown = ", ".join(ns[:4]) + (", ..." if len(ns) > 4 else "")
            print("SKIPPED %d x %s [%s] [%s] -- reason: %s" % (len(ns), cls.split(".")[-1], module, shown, r))

    tot = [0, 0, 0, 0]
    for module, (t, p, fl, s) in per_module.items():
        print("TEST SUMMARY [%s]: tests=%d passed=%d failed=%d skipped=%d" % (module, t, p, fl, s))
        tot = [a + b for a, b in zip(tot, (t, p, fl, s))]
    print("TEST TOTAL [%s]: tests=%d passed=%d failed=%d skipped=%d (from %d result files)" % (
        LEG, tot[0], tot[1], tot[2], tot[3], len(files)))
    return 1 if tot[2] else 0


if __name__ == "__main__":
    sys.exit(main())
