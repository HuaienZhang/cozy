
import math
import tempfile
import os
import subprocess

import plans
from codegen_java import write_java

def _cost(plan, n=float(1000)):
    """Returns (cost,size) tuples"""
    if isinstance(plan, plans.All): return 1, n
    if isinstance(plan, plans.Empty): return 1, 0
    if isinstance(plan, plans.HashLookup):
        cost1, size1 = _cost(plan.plan)
        return cost1 + 1, size1 / 2
    if isinstance(plan, plans.BinarySearch):
        cost1, size1 = _cost(plan.plan)
        return cost1 + (math.log(size1) if size1 >= 1 else 1), size1 / 2
    if isinstance(plan, plans.Filter):
        cost1, size1 = _cost(plan.plan)
        return cost1 + size1, size1 / 2
    if isinstance(plan, plans.Intersect):
        cost1, size1 = _cost(plan.plan1)
        cost2, size2 = _cost(plan.plan2)
        return cost1 + cost2 + size1 + size2, min(size1, size2) / 2
    if isinstance(plan, plans.Union):
        cost1, size1 = _cost(plan.plan1)
        cost2, size2 = _cost(plan.plan2)
        return cost1 + cost2 + size1 + size2, size1 + size2
    raise Exception("Couldn't parse plan: {}".format(plan))

def _dynamic_cost(fields, qvars, plan, benchmark_file):
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "DataStructure.java"), "w") as f:
        write_java(fields, qvars, plan, f.write)

    with open(os.path.join(tmp, "Main.java"), "w") as f:
        f.write("import java.util.*;")
        f.write("\npublic class Main {\n")
        f.write("public static void main(String[] args) { new Main().run(); }\n")
        with open(benchmark_file, "r") as b:
            f.write(b.read())
        f.write("\n}\n")
    print("benchmarking {}[size={}] in {}".format(id(plan), plan.size(), tmp))

    orig = os.getcwd()
    os.chdir(tmp)
    ret = subprocess.call(["javac", "Main.java"])
    assert ret == 0

    java = subprocess.Popen(["java", "Main"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stdin = java.communicate()
    assert java.returncode == 0
    score = long(stdout.strip())

    os.chdir(orig)

    return score

def cost(fields, qvars, plan, cost_model_file):
    if cost_model_file is None:
        return _cost(plan)[0]
    else:
        return _dynamic_cost(fields, qvars, plan, cost_model_file)
