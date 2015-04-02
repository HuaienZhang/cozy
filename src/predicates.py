
from functools import total_ordering
import z3

class Predicate(object):
    def toNNF(self):
        return self
    def toZ3(self, context):
        pass
    def eval(self, env):
        pass
    def size(self):
        pass
    def comparisons(self):
        """returns a stream of var, field tuples"""
        return ()

@total_ordering
class Var(Predicate):
    def __init__(self, name):
        self.name = name
    def toZ3(self, context):
        return z3.Int(self.name, context)
    def eval(self, env):
        return env.get(self.name, 0)
    def size(self):
        return 1
    def __str__(self):
        return str(self.name)
    def __hash__(self):
        return hash(self.name)
    def __eq__(self, other):
        return isinstance(other, Var) and other.name == self.name
    def __lt__(self, other):
        return self.name < other.name if isinstance(other, Var) else type(self) < type(other)

@total_ordering
class Bool(Predicate):
    def __init__(self, val):
        self.val = bool(val)
    def toZ3(self, context):
        return z3.BoolVal(self.val, context)
    def eval(self, env):
        return self.val
    def size(self):
        return 1
    def __str__(self):
        return str(self.val)
    def __hash__(self):
        return hash(self.val)
    def __eq__(self, other):
        return isinstance(other, Bool) and other.val == self.val
    def __lt__(self, other):
        return self.val < other.val if isinstance(other, Bool) else type(self) < type(other)

# operators
Eq = object()
Ne = object()
Gt = object()
Ge = object()
Lt = object()
Le = object()
operators = (Eq, Ne, Gt, Ge, Lt, Le)

def invertOp(op):
    if op is Eq: return Ne
    if op is Ne: return Eq
    if op is Lt: return Ge
    if op is Le: return Gt
    if op is Gt: return Le
    if op is Ge: return Lt

def opToStr(op):
    if op is Eq: return "=="
    if op is Ne: return "!="
    if op is Lt: return "<"
    if op is Le: return "<="
    if op is Gt: return ">"
    if op is Ge: return ">="

def opToName(op):
    if op is Eq: return "Eq"
    if op is Ne: return "Ne"
    if op is Lt: return "Lt"
    if op is Le: return "Le"
    if op is Gt: return "Gt"
    if op is Ge: return "Ge"

@total_ordering
class Compare(Predicate):
    def __init__(self, lhs, op, rhs):
        self.lhs = lhs
        self.op = op
        self.rhs = rhs
    def toNNF(self):
        return Compare(self.lhs.toNNF(), self.op, self.rhs.toNNF())
    def toZ3(self, context):
        if self.op is Eq: return self.lhs.toZ3(context) == self.rhs.toZ3(context)
        if self.op is Ne: return self.lhs.toZ3(context) != self.rhs.toZ3(context)
        if self.op is Ge: return self.lhs.toZ3(context) >= self.rhs.toZ3(context)
        if self.op is Gt: return self.lhs.toZ3(context) >  self.rhs.toZ3(context)
        if self.op is Le: return self.lhs.toZ3(context) <= self.rhs.toZ3(context)
        if self.op is Lt: return self.lhs.toZ3(context) <  self.rhs.toZ3(context)
    def eval(self, env):
        if self.op is Eq: return self.lhs.eval(env) == self.rhs.eval(env)
        if self.op is Ne: return self.lhs.eval(env) != self.rhs.eval(env)
        if self.op is Ge: return self.lhs.eval(env) >= self.rhs.eval(env)
        if self.op is Gt: return self.lhs.eval(env) >  self.rhs.eval(env)
        if self.op is Le: return self.lhs.eval(env) <= self.rhs.eval(env)
        if self.op is Lt: return self.lhs.eval(env) <  self.rhs.eval(env)
    def size(self):
        return 1 + self.lhs.size() + self.rhs.size()
    def comparisons(self):
        return [(self.lhs.name, self.rhs.name)]
    def __str__(self):
        return "{} {} {}".format(self.lhs, opToStr(self.op), self.rhs)
    def __hash__(self):
        return hash((self.lhs, self.op, self.rhs))
    def __eq__(self, other):
        return isinstance(other, Compare) and other.lhs == self.lhs and other.op == self.op and other.rhs == self.rhs
    def __lt__(self, other):
        return (self.lhs, self.op, self.rhs) < (other.lhs, other.op, other.rhs) if isinstance(other, Compare) else type(self) < type(other)

@total_ordering
class And(Predicate):
    def __init__(self, lhs, rhs):
        self.lhs = lhs
        self.rhs = rhs
    def toNNF(self):
        return And(self.lhs.toNNF(), self.rhs.toNNF())
    def toZ3(self, context):
        return z3.And(self.lhs.toZ3(context), self.rhs.toZ3(context), context)
    def eval(self, env):
        return self.lhs.eval(env) and self.rhs.eval(env)
    def size(self):
        return 1 + self.lhs.size() + self.rhs.size()
    def comparisons(self):
        for c in self.lhs.comparisons(): yield c
        for c in self.rhs.comparisons(): yield c
    def __str__(self):
        return "({} and {})".format(self.lhs, self.rhs)
    def __hash__(self):
        return hash((self.lhs, self.rhs))
    def __eq__(self, other):
        return isinstance(other, And) and other.lhs == self.lhs and other.rhs == self.rhs
    def __lt__(self, other):
        return (self.lhs, self.rhs) < (other.lhs, other.rhs) if isinstance(other, And) else type(self) < type(other)

@total_ordering
class Or(Predicate):
    def __init__(self, lhs, rhs):
        self.lhs = lhs
        self.rhs = rhs
    def toNNF(self):
        return Or(self.lhs.toNNF(), self.rhs.toNNF())
    def toZ3(self, context):
        return z3.Or(self.lhs.toZ3(context), self.rhs.toZ3(context), context)
    def eval(self, env):
        return self.lhs.eval(env) or self.rhs.eval(env)
    def size(self):
        return 1 + self.lhs.size() + self.rhs.size()
    def comparisons(self):
        for c in self.lhs.comparisons(): yield c
        for c in self.rhs.comparisons(): yield c
    def __str__(self):
        return "({} or {})".format(self.lhs, self.rhs)
    def __hash__(self):
        return hash((self.lhs, self.rhs))
    def __eq__(self, other):
        return isinstance(other, Or) and other.lhs == self.lhs and other.rhs == self.rhs
    def __lt__(self, other):
        return (self.lhs, self.rhs) < (other.lhs, other.rhs) if isinstance(other, Or) else type(self) < type(other)

@total_ordering
class Not(Predicate):
    def __init__(self, p):
        self.p = p
    def toNNF(self):
        if isinstance(self.p, Var):     return self
        if isinstance(self.p, Bool):    return Bool(not self.p.val)
        if isinstance(self.p, Compare): return Compare(self.p.lhs.toNNF(), invertOp(self.p.op), self.p.rhs.toNNF())
        if isinstance(self.p, And):     return Or(Not(self.p.lhs).toNNF(), Not(self.p.rhs).toNNF())
        if isinstance(self.p, Or):      return And(Not(self.p.lhs).toNNF(), Not(self.p.rhs).toNNF())
        if isinstance(self.p, Not):     return self.p.toNNF()
    def toZ3(self, context):
        return z3.Not(self.p, context)
    def eval(self, env):
        return not self.p.eval(env)
    def size(self):
        return 1 + self.p.size()
    def comparisons(self):
        return self.p.comparisons()
    def __str__(self):
        return "not {}".format(self.p)
    def __hash__(self):
        return hash(self.p) + 1
    def __eq__(self, other):
        return isinstance(other, Not) and other.p == self.p
    def __lt__(self, other):
        return self.p < other.p if isinstance(other, Not) else type(self) < type(other)