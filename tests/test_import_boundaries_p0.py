"""P0 import-boundary skeleton for the framework plan §4 dependency rules.

Minimal skeleton, not a full architecture linter: the probe below parses
every ``companion`` module with :mod:`ast` (all import statements, including
function-level lazy imports) and the plain tests assert only the rules that
were measured to hold on the P0 baseline (151fff5). Rules that do not hold
yet are NOT asserted; they are listed as target rules below.

Measured companion-internal dependency graph on the baseline (lazy imports
included)::

    brain      -> config, multipart, tool_loop   (config: SecretProvider
                  wiring, P5; tool_loop: explicit tool-loop state machine,
                  P4 — brain no longer imports concrete tool modules)
    tool_loop  -> tools.web_search (synthesis cleaning only)
    ingress    -> adapters, brain, config, domain, memory, multipart,
                  ops.connection_health, perception, pipeline, prompt_packs,
                  quota, repetition, safety, services, tool_loop, tools,
                  tools.plugins, tools.read_only, vision   (composition root;
                  ingress.claims -> domain)
    vision     -> adapters (resources 资源分类/摘要口径；本次图片理解接入)
    adapters   -> quota, tools (lazy, only inside OneBotAdapter transport
                  methods; N1: gateway/transport live in adapters;
                  N2: gateway 对新消息动作做派发前内容复核，直接复用
                  multipart.review_bubbles 纯函数 — adapters -> multipart)
    domain     -> (none; pure leaf — P5: claim types sunk here so the
                  in-memory and SQLite claim stores share one definition)
    memory     -> config, domain, multipart (memory.care -> config;
                  memory.claims -> domain, memory.database;
                  cutover parity: memory.ledger imports
                  MAX_ONE_BY_ONE_BUBBLES from multipart — a pure leaf,
                  so no cycle and no upper-layer reach)
    perception -> adapters (P2: parse_event delegates to the adapter
                  normalize, its designated successor; see below)
    safety     -> config, brain, memory, quota, services
                  (safety/__init__ top level: config — P5 wires the
                  default SecretProvider there; lazy in judge: brain)
                  safety.care -> memory.care, quota, safety, services
                  (P5: care sends via DeliveryService, no longer tools)
    services   -> adapters, multipart (delivery.py: SendReceipt/DeliveryHandle
                  types; N2: services.segments 段级规划/递归复核复用
                  multipart 的 review_bubbles/split_plain_text 纯函数)
    tools      -> adapters, config, quota, tools (self only, plus
                  submodules -> tools.read_only)   (P5: tools read secrets
                  via the config-layer SecretProvider, plan §5.9/§6;
                  N1 ruling: the tools client is a legacy shim above the
                  adapter-layer gateway — the platform egress converges
                  in adapters, so tools may depend on adapters)
    config / multipart / ops / prompt_packs (-> config) / quota /
    repetition / scheduler -> no other companion deps beyond config noted

Target rules from P3 onward (not yet true, do not assert yet):

- ``safety`` must not depend on ``brain`` at all: ``ModelLLMJudge.judge``
  currently lazy-imports ``FALLBACK_REPLY`` from ``companion.brain``;
  P3 should move the constant to a leaf module or invert the dependency.
- ``ingress`` must shrink from composition root + processor monolith to a
  thin adapter that depends only on the application layer; business rules
  (safety routing, tool routing, delivery) must not live in ``ingress``.
- ``brain`` must not import concrete tool implementations
  (``tools.current_time`` / ``tools.web_search`` formatting helpers);
  tool-result rendering belongs to the tools/application layer.
  (P4 已达成并已转为断言：渲染与结果政策迁入 tools.plugins，brain
  只依赖 tool_loop 状态机；tool_loop 对 tools.web_search 的依赖
  仅剩合成清洗函数，是后续可继续下沉的已知残留。)
- ``safety.care`` must not import ``tools`` directly; outbound sending goes
  through the shared tool gateway abstraction owned by the application layer.
- ``perception`` was companion-dependency-free at P0; P2 supersedes that:
  its ``parse_event`` now delegates to ``companion.adapters`` (the adapter
  layer is where plan §5.2 puts normalize), so the hard rule from P2 on is
  that ``perception`` may depend on ``adapters`` only.
- No new companion-internal import may be added without updating the
  measured graph in this docstring.
"""

import ast
from pathlib import Path

import pytest

COMPANION_ROOT = Path(__file__).resolve().parents[1] / "companion"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(COMPANION_ROOT).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _companion_top_level_names() -> list[str]:
    """Top-level companion packages/modules as listed on disk.

    Directories carrying an ``__init__.py`` plus the flat ``*.py`` modules
    directly under ``companion/`` (``config``, ``multipart``, ...). Used to
    expand wildcard imports of the ``companion`` package itself.
    """
    names: list[str] = []
    for entry in sorted(COMPANION_ROOT.iterdir()):
        if entry.name == "__pycache__":
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.append(entry.name)
        elif entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
            names.append(entry.stem)
    return names


def _wildcard_target_names(target: str) -> list[str]:
    """Dotted names a ``from <target> import *`` must be charged with.

    A wildcard import must never resolve to an empty dependency set: it
    can bind anything the target package exposes. Wildcards of the
    ``companion`` package itself are therefore expanded to its full
    on-disk top-level listing (every one of those names is a dependency
    the boundary assertions can see); a wildcard of a specific submodule
    is charged with that submodule itself.
    """
    if target == "companion":
        return [f"companion.{name}" for name in _companion_top_level_names()]
    return [target]


def _package_parts(module: str, *, is_package: bool) -> list[str]:
    """Dotted parts of the package containing (or being) ``module``."""
    absolute = "companion" + (f".{module}" if module else "")
    parts = absolute.split(".")
    # A regular module lives *in* its parent package; an __init__ module
    # *is* the package itself.
    return parts if is_package else parts[:-1]


def _resolved_import_names(node: ast.AST, module: str, *, is_package: bool) -> list[str]:
    """Absolute dotted names one import statement refers to.

    Covers the forms a naive ``node.module`` scan misses:

    - ``from companion import ingress`` — the submodule is the alias name,
      not ``node.module`` (which is just ``companion``);
    - ``from ..ingress import X`` — a relative import whose ``node.module``
      (``ingress``) only means something once resolved against the
      importing module's package;
    - ``from .. import ingress`` — ``node.module`` is ``None``; the alias
      names are submodules of the resolved parent package.
    - ``from companion import *`` / ``from .. import *`` — a wildcard has
      no alias names to charge; it is expanded via
      :func:`_wildcard_target_names` so it can never scan as dependency-free.
    """
    names: list[str] = []
    if isinstance(node, ast.Import):
        names.extend(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        wildcard = any(alias.name == "*" for alias in node.names)
        if node.level:
            package = _package_parts(module, is_package=is_package)
            # level 1 is the current package; each extra dot climbs one.
            climb = node.level - 1
            base = package[: len(package) - climb] if climb <= len(package) else []
            if not base:
                return names
            if node.module:
                names.append(".".join([*base, *node.module.split(".")]))
            elif wildcard:
                names.extend(_wildcard_target_names(".".join(base)))
            else:
                names.extend(".".join([*base, alias.name]) for alias in node.names)
        elif node.module:
            names.append(node.module)
            if node.module == "companion":
                if wildcard:
                    # ``from companion import *``: expand to the full
                    # top-level listing instead of charging no alias.
                    names.extend(_wildcard_target_names("companion"))
                else:
                    # ``from companion import <submodule>``: aliases are modules.
                    names.extend(f"companion.{alias.name}" for alias in node.names)
    return names


def _companion_deps(source: str, module: str, *, is_package: bool = False) -> set[str]:
    """Companion top-level packages imported by ``source`` written as ``module``."""
    top = module.split(".")[0] if module else ""
    deps: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _resolved_import_names(node, module, is_package=is_package):
            if name == "companion" or name.startswith("companion."):
                parts = name.split(".")
                dep = parts[1] if len(parts) > 1 else ""
                if dep and dep != top:
                    deps.add(dep)
    return deps


def _companion_imports() -> dict[str, set[str]]:
    """Map each companion module to the companion top-level packages it imports."""
    graph: dict[str, set[str]] = {}
    for path in sorted(COMPANION_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path)
        graph[module] = _companion_deps(
            path.read_text(encoding="utf-8"),
            module,
            is_package=path.name == "__init__.py",
        )
    return graph


GRAPH = _companion_imports()


def _deps(package: str) -> set[str]:
    merged: set[str] = set()
    for module, deps in GRAPH.items():
        if module == package or module.startswith(package + "."):
            merged |= deps
    return merged


@pytest.mark.parametrize(
    "source,module,is_package",
    [
        # Package-level import: the submodule lives in the alias name.
        ("from companion import ingress\n", "safety", False),
        # Relative import with a module part, resolved against the package.
        ("from ..ingress import handle\n", "memory.care", False),
        # Relative package import: node.module is None, aliases are modules.
        ("from .. import ingress\n", "memory.care", False),
        # Absolute submodule import (already covered before the fix).
        ("from companion.ingress import handle\n", "safety", False),
        # Plain dotted import statement.
        ("import companion.ingress\n", "safety", False),
    ],
    ids=["from-companion-import", "relative-module", "relative-package",
         "absolute-submodule", "plain-import"],
)
@pytest.mark.timeout(10)
def test_scanner_detects_ingress_dependency_in_all_import_forms(
    source, module, is_package
):
    """Scanner self-regression: no import spelling may hide a dependency."""
    assert _companion_deps(source, module, is_package=is_package) == {"ingress"}


@pytest.mark.timeout(10)
def test_scanner_detects_absolute_wildcard_import_as_cross_boundary():
    # ``from companion import *`` has no alias names, yet it can bind any
    # already-loaded companion submodule (ingress included). The scanner
    # expands it to the full on-disk top-level listing, so the dependency
    # set is the whole listing minus the importer's own top-level package —
    # never the empty set the pre-fix scanner produced.
    deps = _companion_deps("from companion import *\n", "safety")
    assert deps == set(_companion_top_level_names()) - {"safety"}
    assert "ingress" in deps


@pytest.mark.timeout(10)
def test_scanner_detects_relative_wildcard_import_as_cross_boundary():
    # ``from .. import *`` inside companion.memory.care resolves to the
    # companion package itself, so it carries the same expanded wildcard
    # dependency set as the absolute form (minus the importer's own top).
    deps = _companion_deps("from .. import *\n", "memory.care")
    assert deps == set(_companion_top_level_names()) - {"memory"}
    assert "ingress" in deps and "brain" in deps


@pytest.mark.timeout(10)
def test_scanner_relative_import_within_same_package_is_not_a_dependency():
    # ``from .care import CarePreferences`` inside companion.memory stays
    # inside the memory top-level package, so it is not a cross-boundary dep;
    # likewise a level-1 ``from . import care`` in the package __init__.
    assert _companion_deps(
        "from .care import CarePreferences\n", "memory", is_package=True
    ) == set()
    assert _companion_deps(
        "from . import care\n", "memory", is_package=True
    ) == set()
    # A same-package wildcard (``from . import *`` in the memory package)
    # targets companion.memory itself, so wildcard expansion must not turn
    # it into a cross-boundary dependency either.
    assert _companion_deps("from . import *\n", "memory", is_package=True) == set()


@pytest.mark.timeout(10)
def test_probe_finds_the_companion_packages():
    assert {
        "adapters", "brain", "config", "domain", "ingress", "memory",
        "multipart", "ops", "perception", "pipeline", "prompt_packs",
        "quota", "repetition", "safety", "scheduler", "services",
        "tool_loop", "tools",
    } <= set(GRAPH)


@pytest.mark.timeout(10)
def test_perception_depends_only_on_adapters():
    # P2 ruling: perception delegates parsing to the adapter layer (plan
    # §5.2); adapters is its only allowed companion dependency.
    assert _deps("perception") == {"adapters"}


@pytest.mark.timeout(10)
def test_leaf_modules_import_no_companion_module():
    for package in (
        "config", "domain", "multipart", "ops", "quota", "repetition", "scheduler",
    ):
        assert _deps(package) == set(), package


@pytest.mark.timeout(10)
def test_safety_does_not_depend_on_ingress():
    assert "ingress" not in _deps("safety")


@pytest.mark.timeout(10)
def test_tools_do_not_depend_on_ingress_brain_or_safety():
    # P5 ruling: tools may read the config layer (SecretProvider lives in
    # config per plan §5.9/§6) in addition to quota; upper layers stay banned.
    # N1 ruling (NapCat full-API plan C2/C15): the tools HttpOneBotClient
    # is a legacy-calibre shim above the adapter-layer ActionGateway /
    # OneBotTransport, and the platform egress converges in adapters —
    # so tools may also depend on adapters. This is the only relaxation;
    # every other assertion in this file is unchanged.
    assert _deps("tools") <= {"quota", "config", "adapters"}
    assert not (_deps("tools") & {"ingress", "brain", "safety", "memory", "perception"})


@pytest.mark.timeout(10)
def test_memory_does_not_depend_on_upper_layers():
    # P5 ruling: memory may depend on config and on the pure domain leaf
    # (claim types); it must never reach back into ingress or other
    # application layers.
    # Cutover ruling (production round 7 parity, 09b621c): the delivery
    # ledger's per-turn bubble cap is derived from the splitter's
    # MAX_ONE_BY_ONE_BUBBLES so the two caps can never drift apart;
    # multipart is a pure leaf (imports no companion module, see the leaf
    # test above), so this adds no cycle and no upper-layer reach. This
    # is the only relaxation; every other assertion here is unchanged.
    assert _deps("memory") <= {"config", "domain", "multipart"}
    assert not (_deps("memory") & {"ingress", "brain", "safety", "tools", "perception"})


@pytest.mark.timeout(10)
def test_brain_does_not_depend_on_ingress_safety_or_memory():
    assert not (_deps("brain") & {"ingress", "safety", "memory", "perception", "ops"})


@pytest.mark.timeout(10)
def test_brain_does_not_depend_on_tools():
    # P4 ruling: tool-result rendering and result policies live in
    # tools.plugins; brain reaches tools only through the tool_loop
    # state machine, never by importing companion.tools directly.
    assert "tools" not in _deps("brain")


@pytest.mark.timeout(10)
def test_perception_does_not_depend_on_brain():
    assert "brain" not in _deps("perception")


@pytest.mark.timeout(10)
def test_no_companion_module_imports_ingress():
    for module, deps in GRAPH.items():
        assert "ingress" not in deps, module
