"""Run single rollouts: argv[1] = comma list of task:arm:rep. SUB=oc|cx, NS=namespace suffix."""
import json, os, sys, concurrent.futures as cf
from pathlib import Path
from harness2.cli._common import make_adapter
sub = os.environ.get("SUB", "cx")
ad = make_adapter("wb", "code", sub)
H = {"base": None}
JOBS = [tuple(j.split(":")) for j in sys.argv[1].split(",")]
def run(job):
    task, arm, rep = job
    rd = Path(ad.execute(task, H.get(arm), f"wb-code-{sub}-{os.environ.get('NS','x')}/{task}/{arm}-{rep}", timeout=900))
    rw = json.loads((rd/"verifier/reward.json").read_text()) if (rd/"verifier/reward.json").is_file() else {}
    return dict(task=task, arm=arm, rep=rep, passed=rw.get("tests_passed"), total=rw.get("tests_total"), run_dir=str(rd))
with cf.ThreadPoolExecutor(int(os.environ.get("PAR", "2"))) as ex:
    for r in ex.map(run, JOBS):
        print(json.dumps(r), flush=True)
