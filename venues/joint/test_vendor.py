import json
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "data" / "seeds_takeoff.json"


def test_inference_imports_on_modern_python():
    from venues.joint.inference.factored_market import FactoredMarket  # noqa: F401


def test_build_from_takeoff_seeds():
    from venues.joint.inference.factored_market import FactoredMarket
    seeds = json.loads(SEEDS.read_text())
    # build nodes the same way bayes server does: markets list with cpt/priors
    from venues.joint.venue import nodes_from_seeds  # implemented in this task
    nodes = nodes_from_seeds(seeds)
    fm = FactoredMarket.from_nodes(nodes, liquidity=50.0, max_width=8)
    assert len(fm.variables()) > 900
    m = fm.marginal("ftm_agi_by_2040")
    assert m is not None and 0.0 < m["yes"] < 1.0
