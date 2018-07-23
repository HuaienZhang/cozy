"""Functions for managing stateful computation.

Important functions:
 - mutate: compute the new value of an expression after a statement executes
 - mutate_in_place: write code to keep a derived value in sync with its inputs
"""

import itertools
from collections import OrderedDict

from cozy.common import fresh_name, typechecked
from cozy import syntax
from cozy import target_syntax
from cozy.syntax_tools import free_vars, pprint, fresh_var, strip_EStateVar, subst, qsubst, BottomUpRewriter, alpha_equivalent, break_seq, mk_lambda
from cozy.typecheck import is_numeric, is_collection, retypecheck
from cozy.solver import valid, ModelCachingSolver
from cozy.opts import Option
from cozy.structures import extension_handler
from cozy.evaluation import construct_value, eval
from cozy.contexts import Context, UnderBinder
from cozy.pools import RUNTIME_POOL
from cozy.simplification import simplify, simplify_cond as cond

skip_stateless_synthesis = Option("skip-stateless-synthesis", bool, False,
    description="Do not waste time optimizing expressions that do not depend on the data structure state")
update_numbers_with_deltas = Option("update-numbers-with-deltas", bool, False)

def mutate(e : syntax.Exp, op : syntax.Stm) -> syntax.Exp:
    """Return the new value of `e` after executing `op`."""
    if isinstance(op, syntax.SNoOp):
        return e
    elif isinstance(op, syntax.SAssign):
        return _do_assignment(op.lhs, op.rhs, e)
    elif isinstance(op, syntax.SCall):
        if op.func == "add":
            return mutate(e, syntax.SCall(op.target, "add_all", (syntax.ESingleton(op.args[0]).with_type(op.target.type),)))
        elif op.func == "add_all":
            return mutate(e, syntax.SAssign(op.target, syntax.EBinOp(op.target, "+", op.args[0]).with_type(op.target.type)))
        elif op.func == "remove":
            return mutate(e, syntax.SCall(op.target, "remove_all", (syntax.ESingleton(op.args[0]).with_type(op.target.type),)))
        elif op.func == "remove_all":
            return mutate(e, syntax.SAssign(op.target, syntax.EBinOp(op.target, "-", op.args[0]).with_type(op.target.type)))
        else:
            raise Exception("Unknown func: {}".format(op.func))
    elif isinstance(op, syntax.SIf):
        then_branch = mutate(e, op.then_branch)
        else_branch = mutate(e, op.else_branch)
        if alpha_equivalent(then_branch, else_branch):
            return then_branch
        return syntax.ECond(op.cond, then_branch, else_branch).with_type(e.type)
    elif isinstance(op, syntax.SSeq):
        if isinstance(op.s1, syntax.SSeq):
            return mutate(e, syntax.SSeq(op.s1.s1, syntax.SSeq(op.s1.s2, op.s2)))
        e2 = mutate(mutate(e, op.s2), op.s1)
        if isinstance(op.s1, syntax.SDecl):
            # e2 = syntax.ELet(op.s1.val, syntax.ELambda(
            #     syntax.EVar(op.s1.id).with_type(op.s1.val.type),
            #     e2))
            # var = syntax.EVar(op.s1.id).with_type(op.s1.val.type)
            # e2 = qsubst(e2, var, op.s1.val)
            e2 = subst(e2, {op.s1.id: op.s1.val})
        return e2
    elif isinstance(op, syntax.SDecl):
        return e
    else:
        raise NotImplementedError(type(op))

def simplify_white(s):
    s = s.strip()
    s = s.replace("\n", " ")
    s = s.replace(r"\s+", " ")
    return s

### No, bad idea: between the decl and its use, the vars it mentions may change
# def inline_sdecl(s, env=None):
#     if env is None:
#         env = { }
#     if isinstance(s, syntax.SNoOp):
#         return s
#     if isinstance(s, syntax.SSeq):
#         s1 = inline_sdecl(s.s1, env)
#         s2 = inline_sdecl(s.s2, env)
#         return syntax.SSeq(s1, s2)
#     if isinstance(s, syntax.SIf):
#         return syntax.SIf(
#             subst(s.cond, env),
#             inline_sdecl(s.then_branch, dict(env)),
#             inline_sdecl(s.else_branch, dict(env)))
#     if isinstance(s, syntax.SCall):
#         return syntax.SCall(s.target, s.func, tuple(subst(a, env) for a in s.args))
#     if isinstance(s, syntax.SAssign):
#         return syntax.SAssign(s.lhs, subst(s.rhs, env))
#     if isinstance(s, syntax.SDecl):
#         env[s.id] = subst(s.val, env)
#         return syntax.SNoOp()
#     raise NotImplementedError(type(s))

def differences(e1, e2):
    from cozy.common import ADT
    if isinstance(e1, syntax.ELambda):
        return
    if isinstance(e1, ADT) and type(e1) is type(e2):
        for c1, c2 in zip(e1.children(), e2.children()):
            yield from differences(c1, c2)
    yield e1, e2

@typechecked
def assert_eq(e1 : syntax.Exp, e2 : syntax.Exp, context : Context, assumptions : syntax.Exp = syntax.T):
    formula = syntax.EAll([assumptions, syntax.ENot(syntax.EEq(e1, e2))])
    model = minimal_model(formula)
    if model is not None:
        from cozy.evaluation import eval
        from cozy.contexts import shred
        from cozy.common import unique

        fvs = free_vars(formula)
        pprint_model(model, { v.id : v.type for v in fvs })
        # print("--->")
        # for v in fvs:
        #     print("  {} = {}".format(v.id, pprint_value(v.type, eval(mutate(v, op), model))))

        import itertools
        for x1, x2 in unique(itertools.chain([(e1, e2)], differences(e1, e2))):

            print("=" * 20)

            print(pprint(x1))
            print(pprint(x2))

            print("expected: {}".format(pprint_value(x1.type, eval(x1, model))))
            print("got: {}".format(pprint_value(x2.type, eval(x2, model))))

            # for x, x_ctx, _ in unique(shred(e2, context)):
            #     if x_ctx == context:
            #         print("=" * 20)
            #         print("{} ----> {}".format(pprint(x), pprint_value(x.type, eval(x, model))))

        raise AssertionError()

def statevar(e):
    assert not isinstance(e, target_syntax.EStateVar)
    return target_syntax.EStateVar(e).with_type(e.type)

@typechecked
def better_mutate(
        e           : target_syntax.EStateVar,
        context     : Context,
        op          : syntax.Stm,
        assumptions : syntax.Exp = syntax.T) -> syntax.Exp:
    """
    NOTES
        - e is runtime exp
        - output is runtime exp
    """
    # print("{} X {}".format(pprint(e), simplify_white(pprint(op))))

    if alpha_equivalent(e.e, mutate(e.e, op)):
        return e

    # special case on the structure of e
    esv = e
    e = e.e

    if isinstance(e.type, syntax.TBag) or isinstance(e.type, syntax.TSet):
        add, remove = checked_bag_delta(e, context, op, assumptions)
        print("{} x {} ---> +{}, -{}".format(pprint(e), pprint(op), pprint(add), pprint(remove)))
        return bag_union(bag_subtract(context, assumptions, e, remove), add)

    if isinstance(e, syntax.EUnaryOp):
        if e.op == syntax.UOp.Exists:
            add, remove = checked_bag_delta(e.e, context, op, assumptions)
            return syntax.EGt(
                target_syntax.EStateVar(len_of(e.e)).with_type(syntax.INT) + len_of(add),
                len_of(remove))
        if e.op == syntax.UOp.Not:
            new_val = better_mutate(statevar(e.e), context, op, assumptions)
            return syntax.ENot(new_val)

    if isinstance(e, syntax.ESingleton):
        return syntax.ESingleton(
            better_mutate(statevar(e.e), context, op)).with_type(e.type)

    if isinstance(e, syntax.EBinOp):
        return syntax.EBinOp(
            better_mutate(statevar(e.e1), context, op, assumptions), e.op,
            better_mutate(statevar(e.e2), context, op, assumptions)).with_type(e.type)

    if isinstance(e, syntax.ECall):
        return syntax.ECall(e.func,
            tuple(better_mutate(statevar(x), context, op, assumptions) for x in e.args)).with_type(e.type)

    if isinstance(e, syntax.ECond):
        then_branch = better_mutate(statevar(e.then_branch), context, op, assumptions)
        else_branch = better_mutate(statevar(e.else_branch), context, op, assumptions)
        return cond(e.cond,
            cond(became_false(e.cond, context, op, assumptions), else_branch, then_branch),
            cond(became_true (e.cond, context, op, assumptions), then_branch, else_branch))

    # if bag:
    #     added+removed
    if isinstance(e, syntax.EArgMin) or isinstance(e, syntax.EArgMax):
        if alpha_equivalent(e.f, mutate(e.f, op)):
            to_add, to_del = checked_bag_delta(target_syntax.EStateVar(e.e).with_type(e.e.type), context, op, assumptions)
            from cozy.structures.heaps import to_heap, EHeapPeek2
            h = to_heap(e)
            h = target_syntax.EStateVar(h).with_type(h.type)
            second_min = EHeapPeek2(h, target_syntax.EStateVar(syntax.ELen(e.e)).with_type(syntax.INT)).with_type(e.type)

            v = fresh_var(to_del.type, "removed")

            if isinstance(to_del, syntax.EEmptyList) or valid(syntax.EImplies(assumptions, syntax.EEmpty(to_del))):
                min_after_del = esv
            # elif valid(syntax.EImplies(assumptions, syntax.EEq(syntax.ELen(to_del), syntax.ONE))):
            #     min_after_del = second_min
            # elif valid(syntax.EImplies(assumptions, syntax.ELe(syntax.ELen(to_del), syntax.ONE))):
            #     min_after_del = syntax.ECond(
            #         syntax.ELet(
            #             to_del,
            #             syntax.ELambda(v,
            #                 syntax.EAll([syntax.EExists(v), syntax.EEq(syntax.EUnaryOp(syntax.UOp.The, v).with_type(e.type), esv)]))).with_type(syntax.BOOL),
            #         syntax.ECond(syntax.EGt(target_syntax.EStateVar(syntax.ELen(e.e)).with_type(syntax.INT), syntax.ONE), second_min, syntax.EEmptyList().with_type(e.e.type)).with_type(e.e.type),
            #         syntax.ECond(target_syntax.EStateVar(syntax.EExists(e.e)).with_type(syntax.BOOL), syntax.ESingleton(esv).with_type(e.e.type), syntax.EEmptyList().with_type(e.e.type)).with_type(e.e.type)).with_type(e.e.type)
            #     assert_eq(
            #         type(e)(bag_subtract(e.e, to_del), e.f).with_type(e.type),
            #         type(e)(min_after_del, e.f).with_type(e.type),
            #         context=context,
            #         assumptions=assumptions)
            elif valid(syntax.EImplies(assumptions, syntax.EEq(syntax.ELen(to_del), syntax.ONE))):
                min_after_del = syntax.ECond(
                    syntax.EEq(syntax.EUnaryOp(syntax.UOp.The, to_del).with_type(e.type), esv),
                    second_min,
                    esv).with_type(e.e.type)
            else:
                # ugh, recompute
                print("assuming {}".format(pprint(assumptions)))
                print("deleting {}".format(pprint(to_del)))

                model = minimal_model(syntax.EAll([assumptions, syntax.ENot(syntax.ELe(syntax.ELen(to_del), syntax.ONE))]), solver=solver_for(context))
                print("Ooops!")
                print("  assuming {}".format(pprint(assumptions)))
                print("  e.type is {}".format(pprint(e.type)))
                print("  d  = {}".format(pprint(to_del)))
                print("  n  = {}".format(pprint(to_add)))
                print("del         : {}".format(eval(to_del, model)))
                print("add         : {}".format(eval(to_add, model)))
                print("true del    : {}".format(eval(to_bag(e.e) - to_bag(mutate(e.e, op)), model)))
                print("true add    : {}".format(eval(to_bag(mutate(e.e, op)) - to_bag(e.e), model)))

                raise NotEfficient(e)
                return mutate(e, op)

            if definitely(is_empty(to_add)):
                res = min_after_del
            else:
                res = type(e)(bag_union(
                    syntax.ESingleton(min_after_del).with_type(syntax.TBag(min_after_del.type)),
                    to_add), e.f).with_type(e.type)

            # assert retypecheck(res)
            return res
        else:
            raise NotEfficient(e)

    # # take care of basic statement forms
    # if isinstance(op, syntax.SNoOp):
    #     return e
    # if isinstance(op, syntax.SSeq):
    #     e = better_mutate(e, context, op.s1)
    #     e = better_mutate_statevars(e, context, op.s2)
    #     return e
    # if isinstance(op, syntax.SIf):
    #     return syntax.ECond(
    #         op.cond,
    #         better_mutate(e, context, op.then_branch),
    #         better_mutate(e, context, op.else_branch)).with_type(e.type)
    # if isinstance(op, syntax.SDecl):
    #     raise ValueError()

    # print("e = {!r}".format(e))
    # print("context = {!r}".format(context))
    # print("op = {!r}".format(op))
    # print("env = {!r}".format(env))
    print(pprint(e))
    print(pprint(op))
    raise NotImplementedError(pprint(e))

# def invert(f : syntax.ELambda, domain : syntax.Exp) -> syntax.ELambda:
#     """Invert f over the given domain."""
#     raise NotImplementedError(pprint(f))

from cozy.syntax_tools import compose

def emap(e, f):
    if f.body == f.arg:
        return e
    out_type = type(e.type)(f.body.type)
    if isinstance(e, syntax.EEmptyList):
        return syntax.EEmptyList().with_type(out_type)
    if isinstance(e, syntax.ESingleton):
        return syntax.ESingleton(f.apply_to(e.e)).with_type(out_type)
    if isinstance(e, syntax.EBinOp) and e.op == "+":
        return bag_union(emap(e.e1, f), emap(e.e2, f))
    if isinstance(e, target_syntax.EMap):
        return emap(e.e, compose(f, e.f))
    if isinstance(e, syntax.ECond):
        return cond(e.cond,
            emap(e.then_branch, f),
            emap(e.else_branch, f))
    return target_syntax.EMap(e, f).with_type(out_type)

def efilter(e, f):
    if f.body == syntax.T:
        return e
    out_type = e.type
    if isinstance(e, syntax.EEmptyList):
        return syntax.EEmptyList().with_type(out_type)
    if isinstance(e, syntax.EBinOp) and e.op == "+":
        return bag_union(efilter(e.e1, f), efilter(e.e2, f))
    if isinstance(e, target_syntax.EMap):
        return efilter(e.e, compose(f, e.f))
    if isinstance(e, syntax.ESingleton):
        return cond(
            simplify(f.apply_to(e.e)),
            e,
            syntax.EEmptyList().with_type(e.type))
    return target_syntax.EFilter(e, f).with_type(out_type)

def is_filter(f_arg, f_body):
    if isinstance(f_body, syntax.ECond):
        return both(
            is_filter(f_arg, f_body.then_branch),
            is_filter(f_arg, f_body.else_branch))
    if isinstance(f_body, syntax.EEmptyList):
        return True
    if isinstance(f_body, syntax.ESingleton) and f_body.e == f_arg:
        return True
    return MAYBE

def to_predicate(f_body):
    # PRE: is_filter(?, f_body)
    if isinstance(f_body, syntax.ECond):
        return cond(f_body.cond,
            to_predicate(f_body.then_branch),
            to_predicate(f_body.else_branch))
    if isinstance(f_body, syntax.EEmptyList):
        return syntax.F
    if isinstance(f_body, syntax.ESingleton):
        return syntax.T
    raise ValueError(cond)

def infer_the_one_passing_element(f_arg, f_body):
    from cozy.syntax_tools import break_conj
    f_body = nnf(f_body)
    for part in break_conj(f_body):
        print(" * {}".format(pprint(part)))
        if isinstance(part, syntax.EBinOp) and part.op in ("==", "==="):
            for e1, e2 in itertools.permutations([part.e1, part.e2]):
                if e1 == f_arg and f_arg not in free_vars(e2):
                    return e2
    return None

def are_unique(xs):
    if isinstance(xs, syntax.EEmptyList):
        return True
    if isinstance(xs, syntax.ESingleton):
        return True
    if isinstance(xs, syntax.EUnaryOp) and xs.op == syntax.UOp.Distinct:
        return True
    return MAYBE

def flatmap(context, assumptions, e, f):
    if isinstance(f.body, syntax.ESingleton) and f.body.e == f.arg:
        return e
    if isinstance(e, syntax.EEmptyList) or isinstance(f.body, syntax.EEmptyList):
        return syntax.EEmptyList().with_type(f.body.type)
    if isinstance(e, syntax.ESingleton):
        res = simplify(f.apply_to(e.e))
        if isinstance(res, syntax.ECond):
            return fork(context, assumptions, res.cond,
                res.then_branch,
                res.else_branch)
        return res
    if isinstance(e, syntax.EBinOp) and e.op == "+":
        return bag_union(flatmap(context, assumptions, e.e1, f), flatmap(context, assumptions, e.e2, f))
    if isinstance(e, syntax.ECond):
        return fork(context, assumptions, e.cond,
            flatmap(context, assumptions, e.then_branch, f),
            flatmap(context, assumptions, e.else_branch, f))
    # if definitely(is_filter(f.arg, f.body)):
    #     print("FILTER: {}".format(pprint(f.body)))
    #     if definitely(are_unique(e)):
    #         print("UNIQUE: {}".format(pprint(e)))
    #         x = infer_the_one_passing_element(f.arg, to_predicate(f.body))
    #         if x is not None:
    #             e = syntax.ESingleton(x).with_type(e.type)
    #         else:
    #             raise ValueError(pprint(f.body))
    return target_syntax.EFlatMap(e, f).with_type(f.body.type)

def bag_union(e1, e2):
    if isinstance(e1, syntax.EEmptyList):
        return e2
    if isinstance(e2, syntax.EEmptyList):
        return e1
    return syntax.EBinOp(e1, "+", e2).with_type(e1.type)

def bag_intersection(context, assumptions, e1, e2):
    if isinstance(e1, syntax.EEmptyList):
        return e1
    if isinstance(e2, syntax.EEmptyList):
        return e2
    if isinstance(e1, syntax.ECond):
        return fork(context, assumptions, e1.cond,
            bag_intersection(context, assumptions, e1.then_branch, e2),
            bag_intersection(context, assumptions, e1.else_branch, e2))
    if isinstance(e2, syntax.ECond):
        return fork(context, assumptions, e2.cond,
            bag_intersection(context, assumptions, e1, e2.then_branch),
            bag_intersection(context, assumptions, e1, e2.else_branch))
    if isinstance(e1, syntax.ESingleton) and isinstance(e2, syntax.ESingleton):
        return fork(context, assumptions, equal(e1.e, e2.e), e1, syntax.EEmptyList().with_type(e1.type))
    # if isinstance(e1, target_syntax.EFilter):
    #     return efilter(bag_intersection(context, assumptions, e1.e, e2), e1.p)
    # if isinstance(e2, target_syntax.EFilter):
    #     return efilter(bag_intersection(context, assumptions, e1, e2.e), e2.p)
    return syntax.EIntersect(e1, e2)

class NotEfficient(Exception):
    def __init__(self, e):
        super().__init__(pprint(e))
        self.expression = e

def bag_contains(bag, x):
    if definitely(element_of(x, bag)):
        return syntax.T
    if isinstance(bag, target_syntax.EFilter):
        return syntax.EAll([
            bag_contains(bag.e, x),
            bag.p.apply_to(x)])
    if isinstance(bag, syntax.ESingleton):
        return equal(bag.e, x)
    return syntax.EIn(x, bag)

def bag_subtract(context, assumptions, e1, e2):
    if isinstance(e1, syntax.EEmptyList):
        return e1
    if isinstance(e2, syntax.EEmptyList):
        return e1
    if alpha_equivalent(e1, e2):
        return syntax.EEmptyList().with_type(e1.type)
    if isinstance(e2, syntax.ECond):
        return fork(context, assumptions, e2.cond,
            bag_subtract(context, assumptions, e1, e2.then_branch),
            bag_subtract(context, assumptions, e1, e2.else_branch)).with_type(e1.type)
    if isinstance(e1, syntax.ECond):
        return fork(context, assumptions, e1.cond,
            bag_subtract(context, assumptions, e1.then_branch, e2),
            bag_subtract(context, assumptions, e1.else_branch, e2)).with_type(e1.type)
    # if isinstance(e1, syntax.EBinOp) and e1.op == "+" and alpha_equivalent(e1.e1, e2):
    #     return e1.e2
    # if isinstance(e2, syntax.EBinOp) and e2.op == "+" and alpha_equivalent(e1, e2.e1):
    #     return syntax.EEmptyList().with_type(e1.type)
    if isinstance(e2, syntax.EBinOp) and e2.op == "+":
        return bag_subtract(context, assumptions, bag_subtract(context, assumptions, e1, e2.e1), e2.e2)
    if isinstance(e1, syntax.EBinOp) and e1.op == "-" and alpha_equivalent(e1.e1, e2):
        return syntax.EEmptyList().with_type(e1.type)
    if isinstance(e2, syntax.EBinOp) and e2.op == "-" and alpha_equivalent(e2.e1, e1) and isinstance(e2.e2, syntax.ESingleton):
        return fork(context, assumptions, bag_contains(e1, e2.e2.e), e2.e2, syntax.EEmptyList().with_type(e1.type))
    # if isinstance(e1, syntax.EBinOp) and e1.op == "+" and isinstance(e1.e2, syntax.ESingleton):
    #     return cond(
    #         bag_contains(e2, e1.e2.e),
    #         bag_subtract(context, assumptions, e1.e1, bag_subtract(context, assumptions, e2, e1.e2)),
    #         bag_union(bag_subtract(context, assumptions, e1.e1, e2), e1.e2.e))
    if isinstance(e1, syntax.ESingleton) and isinstance(e2, syntax.ESingleton):
        return fork(context, assumptions, equal(e1.e, e2.e),
            syntax.EEmptyList().with_type(e1.type),
            e1)
    # if isinstance(e2, syntax.ESingleton):
    #     return syntax.EBinOp(e1, "-", e2).with_type(e1.type)
    # raise NotEfficient(syntax.EBinOp(e1, "-", e2).with_type(e1.type))
    return syntax.EBinOp(e1, "-", e2).with_type(e1.type)

# from collections import namedtuple
# SimpleAssignment = namedtuple("SimpleAssignment", ["lval", "rhs"])

@typechecked
def flatten(s : syntax.Stm, so_far : syntax.Stm = syntax.SNoOp(), pc : syntax.Exp = syntax.T):

    if isinstance(s, syntax.SNoOp) or isinstance(s, syntax.SDecl):
        pass

    elif isinstance(s, syntax.SSeq):
        yield from flatten(s.s1, so_far=so_far, pc=pc)
        yield from flatten(s.s2, so_far=syntax.SSeq(so_far, s.s1), pc=pc)

    elif isinstance(s, syntax.SIf):
        cond = mutate(s.cond, so_far)
        yield from flatten(s.then_branch, so_far=so_far, pc=syntax.EAll([pc, cond]))
        yield from flatten(s.else_branch, so_far=so_far, pc=syntax.EAll([pc, syntax.ENot(cond)]))

    elif isinstance(s, syntax.SCall):
        if s.func == "add":
            yield from flatten(syntax.SCall(s.target, "add_all", (syntax.ESingleton(s.args[0]).with_type(s.target.type),)), so_far=so_far, pc=pc)
        elif s.func == "add_all":
            v = fresh_var(s.target.type.t)
            arg = efilter(mutate(s.args[0], so_far), syntax.ELambda(v, pc))
            yield syntax.SCall(s.target, s.func, (arg,))
        elif s.func == "remove":
            yield from flatten(syntax.SCall(s.target, "remove_all", (syntax.ESingleton(s.args[0]).with_type(s.target.type),)), so_far=so_far, pc=pc)
        elif s.func == "remove_all":
            v = fresh_var(s.target.type.t)
            arg = efilter(mutate(s.args[0], so_far), syntax.ELambda(v, pc))
            yield syntax.SCall(s.target, s.func, (arg,))
        else:
            raise ValueError(s.func)

    elif isinstance(s, syntax.SAssign):
        yield syntax.SAssign(s.lhs, mutate(s.rhs, so_far))

    else:
        raise NotImplementedError(s)


def as_bag(e):
    if isinstance(e.type, syntax.TList):
        elem_type = e.type.t
        return target_syntax.EFilter(e, syntax.ELambda(syntax.EVar("x").with_type(elem_type), syntax.T)).with_type(syntax.TBag(elem_type))
    return e

def checked_bag_delta(e, context, s, assumptions : syntax.Exp = syntax.T):
    added, removed = bag_delta(e, context, s, assumptions)
    # if not definitely(singleton_or_empty(added)):
    #     raise ValueError()
    # if not definitely(singleton_or_empty(removed)):
    #     raise ValueError()
    # if valid(syntax.EImplies(assumptions, syntax.EEmpty(added))):
    #     added = syntax.EEmptyList().with_type(added.type)
    # if valid(syntax.EImplies(assumptions, syntax.EEmpty(removed))):
    #     removed = syntax.EEmptyList().with_type(removed.type)
    return (added, removed)
    from cozy.contexts import RootCtx
    n, d = tup
    new_e = mutate(e, s)
    try:
        assert_eq(as_bag(syntax.EBinOp(new_e, "-", e).with_type(e.type)), as_bag(n), context=RootCtx((), ()))
        assert_eq(as_bag(syntax.EBinOp(e, "-", new_e).with_type(e.type)), as_bag(d), context=RootCtx((), ()))
    except:
        print("=" * 20)
        print("exp: {}".format(pprint(e)))
        print("stm:")
        print(pprint(s))
        raise
    return tup

class MaybeType(object):
    def __bool__(self):
        raise ValueError("attempt to convert Maybe to a bool")

MAYBE = MaybeType()

def definitely(v):
    if isinstance(v, MaybeType):
        return False
    return bool(v)

def possibly(v):
    if isinstance(v, MaybeType):
        return True
    return bool(v)

def both(x, y):
    if definitely(x) and definitely(y):
        return True
    if possibly(x) and possibly(y):
        return MAYBE
    return False

def invert(x):
    if definitely(x):
        return False
    if not possibly(x):
        return True
    return MAYBE

def singleton_or_empty(e):
    if isinstance(e, syntax.EEmptyList) or isinstance(e, syntax.ESingleton):
        return True
    if isinstance(e, target_syntax.EStateVar):
        return singleton_or_empty(e.e)
    if isinstance(e, target_syntax.EMap):
        return singleton_or_empty(e.e)
    if isinstance(e, target_syntax.EFilter):
        return singleton_or_empty(e.e)
    if isinstance(e, target_syntax.EFlatMap):
        return both(singleton_or_empty(e.e), singleton_or_empty(e.f.body))
    if isinstance(e, syntax.EUnaryOp) and e.op == syntax.UOp.Distinct:
        return singleton_or_empty(e.e)
    if isinstance(e, syntax.ECond):
        then_case = singleton_or_empty(e.then_branch)
        else_case = singleton_or_empty(e.else_branch)
        if definitely(then_case) and definitely(else_case):
            return True
        if possibly(then_case) or possibly(else_case):
            return MAYBE
        return False
    return MAYBE

def is_singleton(e):
    if isinstance(e, syntax.ESingleton):
        return True
    if isinstance(e, target_syntax.EMap):
        return is_singleton(e.e)
    if isinstance(e, syntax.EUnaryOp) and e.op == syntax.UOp.Distinct:
        return is_singleton(e.e)
    return MAYBE

def is_empty(e):
    if isinstance(e, syntax.EEmptyList):
        return True
    if isinstance(e, target_syntax.EMap):
        return is_empty(e.e)
    if isinstance(e, target_syntax.EFilter):
        return is_empty(e.e)
    if isinstance(e, syntax.EUnaryOp) and e.op == syntax.UOp.Distinct:
        return is_empty(e.e)
    return MAYBE

def edistinct(e):
    if definitely(singleton_or_empty(e)):
        return e
    return syntax.EUnaryOp(syntax.UOp.Distinct, e).with_type(e.type)

def equal(e1, e2):
    if isinstance(e1, syntax.EMakeRecord) and isinstance(e2, syntax.EMakeRecord):
        return syntax.EAll([
            simplify(equal(dict(e1.fields)[f], dict(e2.fields)[f]))
            for f, ft in e1.type])
    return syntax.EEq(e1, e2)

def not_equal(e1, e2):
    return syntax.ENe(e1, e2)

def exists(e):
    assert is_collection(e.type)
    if definitely(is_empty(e)):
        return syntax.F
    if definitely(is_singleton(e)):
        return syntax.T
    if isinstance(e, syntax.EBinOp) and e.op == "+":
        return syntax.EAny([
            exists(e.e1),
            exists(e.e2)])
    if isinstance(e, syntax.ECond):
        return cond(e.cond,
            exists(e.then_branch),
            exists(e.else_branch))
    return syntax.EExists(e)

def len_of(e):
    if definitely(singleton_or_empty(e)):
        return cond(exists(e), syntax.ONE, syntax.ZERO)
    if isinstance(e, target_syntax.EMap):
        return len_of(e.e)
    if isinstance(e, syntax.ECond):
        return cond(e.cond,
            len_of(e.then_branch),
            len_of(e.else_branch))
    return syntax.ELen(e)

def is_zero(e):
    if isinstance(e, syntax.ECond):
        return cond(e.cond, is_zero(e.then_branch), is_zero(e.else_branch))
    return syntax.EEq(e, syntax.ZERO)

from cozy.syntax_tools import nnf

def changed(e, context, s, assumptions):
    if alpha_equivalent(e, mutate(e, s)):
        return syntax.F

    if isinstance(e, syntax.EUnaryOp):
        if e.op == syntax.UOp.Not:
            return changed(e.e, context, s, assumptions)
        # if e.op == syntax.UOp.Exists:
        #     n, d = bag_delta(e.e, s)
        #     return cond(e,
        #         is_zero(len_of(e.e) - len_of(d) + len_of(n)),
        #         syntax.EUnaryOp(syntax.UOp.Exists, n))

    if isinstance(e, syntax.ESingleton):
        return changed(e.e, context, s, assumptions)

    # if isinstance(e, syntax.EBinOp):
    #     if e.op == syntax.BOp.Or:
    #         return changed(syntax.ECond(e.e1, syntax.T, e.e2).with_type(syntax.BOOL), context, s)
    #     if e.op == syntax.BOp.And:
    #         return changed(syntax.ECond(e.e1, e.e2, syntax.F).with_type(syntax.BOOL), context, s)

    # if isinstance(e, syntax.ECond):
    #     return cond(changed(e.cond, context, s),
    #         cond(e.cond,
    #             not_equal(e.then_branch, mutate(e.else_branch, s)),  # transition T -> F
    #             not_equal(e.else_branch, mutate(e.then_branch, s))), # transition F -> T
    #         cond(e.cond,
    #             changed(e.then_branch, context, s),
    #             changed(e.else_branch, context, s)))

    return syntax.ENe(e, better_mutate(target_syntax.EStateVar(e).with_type(e.type), context, s, assumptions))
    raise NotImplementedError(pprint(e))

def element_of(x, xs):
    if isinstance(xs, target_syntax.EFilter):
        return element_of(x, xs.e)
    if isinstance(xs, syntax.EEmptyList):
        return False
    if isinstance(x, syntax.EUnaryOp) and x.op == syntax.UOp.The:
        return both(subset_of(x.e, xs), invert(is_empty(x.e)))
    return MAYBE

def subset_of(xs, ys):
    if alpha_equivalent(xs, ys):
        return True
    if isinstance(xs, syntax.EEmptyList):
        return True
    if isinstance(xs, syntax.ECond):
        s1 = subset_of(xs.then_branch, ys)
        s2 = subset_of(xs.else_branch, ys)
        if definitely(s1) and definitely(s2):
            return True
        if possibly(s1) and possibly(s2):
            return MAYBE
        return False
    if isinstance(xs, target_syntax.ESingleton):
        return element_of(xs.e, ys)
    if isinstance(xs, target_syntax.EFilter):
        return subset_of(xs.e, ys)
    if isinstance(xs, target_syntax.EMap) and xs.f.arg == xs.f.body:
        return subset_of(xs.e, ys)
    if isinstance(ys, target_syntax.EMap) and ys.f.arg == ys.f.body:
        return subset_of(xs, ys.e)
    return MAYBE

def to_bag(e):
    x = syntax.EVar("x").with_type(e.type.t)
    return target_syntax.EMap(e, syntax.ELambda(x, x)).with_type(syntax.TBag(e.type.t))

def became_true(e, context, s, assumptions):
    return became_bool(e, context, s, True, assumptions)

def became_false(e, context, s, assumptions):
    return became_bool(e, context, s, False, assumptions)

def check_valid(context, formula, debug={}):
    model = minimal_model(syntax.ENot(formula), solver=solver_for(context))
    if model is not None:
        print("{} is not valid".format(pprint(formula)))
        print("in model {}".format(model))
        for thing, exp in debug.items():
            print(" - {} = {}".format(thing, eval(exp, model)))
        raise AssertionError()

def checked_become_bool(f):
    def g(e, context, s, val, assumptions):
        res = f(e, context, s, val, assumptions)
        # check_valid(context, syntax.EImplies(assumptions,
        #     syntax.EImplies(syntax.EEq(e, syntax.EBool(not val).with_type(syntax.BOOL)),
        #         syntax.EEq(
        #             res,
        #             syntax.EEq(mutate(e, s), syntax.EBool(val).with_type(syntax.BOOL))))))
        return res
    return g

def nonnegative(x):
    if isinstance(x, syntax.EUnaryOp) and x.op == syntax.UOp.Length:
        return True
    if isinstance(x, syntax.EBinOp) and x.op == "+":
        return both(nonnegative(x.e1), nonnegative(x.e2))
    return MAYBE

def ge(x, y):
    if isinstance(x, syntax.ECond):
        return cond(x.cond,
            ge(x.then_branch, y),
            ge(x.else_branch, y))
    if isinstance(y, syntax.ECond):
        return cond(y.cond,
            ge(x, y.then_branch),
            ge(x, y.else_branch))
    if definitely(nonnegative(x)) and y == syntax.ZERO:
        return syntax.T
    if x == syntax.ZERO and isinstance(y, syntax.EUnaryOp) and y.op == syntax.UOp.Length:
        return syntax.ENot(exists(y.e))
    return syntax.EGe(x, y)

@checked_become_bool
def became_bool(e, context, s, val, assumptions):
    """
    Assuming that boolean expression `e` evaluates to (not val),
    does it evaluate to (val) after executing s?
    """
    if alpha_equivalent(e, mutate(e, s)):
        return syntax.F

    if isinstance(e, syntax.EUnaryOp):
        if e.op == syntax.UOp.Not:
            return became_bool(e.e, context, s, not val, assumptions)
        if e.op == syntax.UOp.Exists:
            added, removed = bag_delta(e.e, context, s, assumptions)
            if val:
                return exists(added)
            else:
                return ge(
                    len_of(removed),
                    syntax.ESum([len_of(added), len_of(e.e)]))
    if isinstance(e, syntax.EBinOp):
        if e.op == syntax.BOp.And:
            if val:
                raise NotImplementedError(pprint(e))
                return syntax.EAll([
                    became_bool(e.e1, context, s, val, assumptions),
                    became_bool(e.e2, context, s, val, assumptions)])
            else:
                return syntax.EAny([
                    became_bool(e.e1, context, s, val, assumptions),
                    became_bool(e.e2, context, s, val, assumptions)])
        if e.op == syntax.BOp.Or:
            if val:
                return syntax.EAny([
                    became_bool(e.e1, context, s, val, assumptions),
                    became_bool(e.e2, context, s, val, assumptions)])
            else:
                # one (both?) of these are true...
                return cond(e.e1,
                    cond(e.e2,
                        syntax.EAll([
                            became_bool(e.e1, context, s, val, assumptions),
                            became_bool(e.e2, context, s, val, assumptions)]),
                        became_bool(e.e1, context, s, val, assumptions)),
                    became_bool(e.e2, context, s, val, assumptions))
    raise NotImplementedError(pprint(e))

def implies(e1, e2):
    if e1 == syntax.T:
        return e2
    if e1 == syntax.F:
        return syntax.T
    if e2 == syntax.T:
        return syntax.T
    if e2 == syntax.F:
        return syntax.ENot(e1)
    return syntax.EImplies(e1, e2)

def dbg(f):
    def g(e, context, s, assumptions : syntax.Exp = syntax.T):
        res = f(e, context, s, assumptions)
        n, d = res
        print("delta {}: +{}, -{}".format(
            pprint(e), pprint(n), pprint(d)))

        if d.size() > 100:
            raise ValueError()

        # check_valid(context, syntax.EImplies(assumptions, syntax.EIsSubset(d, e)),
        #     debug={
        #         "bag": e,
        #         "deleted": d,
        #         "true deleted": e - mutate(e, s)})
        # check_valid(context, syntax.EImplies(assumptions, target_syntax.EDisjoint(n, d)),
        #     debug={
        #         "bag": e,
        #         "deleted": d,
        #         "true deleted": e - mutate(e, s),
        #         "added": n,
        #         "true added": mutate(e, s) - e})
        # check_valid(context, syntax.EImplies(
        #     assumptions,
        #     syntax.ELe(syntax.ELen(d), syntax.ONE)),
        #     debug={
        #     "bag": e,
        #     "bag_prime": mutate(e, s),
        #     "removed": d,
        #     "added": n,
        #     })

        if False:
            e_prime = mutate(e, s)
            interp = e - d + n
            model = solver_for(context).satisfy(syntax.EAll([assumptions, syntax.ENot(syntax.EEq(
                to_bag(e_prime),
                to_bag(interp)))]))
            if isinstance(e, syntax.EUnaryOp) and e.op == syntax.UOp.Distinct:
                einner = e.e
                n2, d2 = bag_delta(einner, context, s, assumptions)
            else:
                einner = n2 = d2 = syntax.ZERO
            if model is not None:
                print("Ooops!")
                print("  assuming {}".format(pprint(assumptions)))
                print("  e.type is {}".format(pprint(e.type)))
                print("  e  = {}".format(pprint(e)))
                print("  e' = {}".format(pprint(e_prime)))
                print("  d  = {}".format(pprint(d)))
                print("  n  = {}".format(pprint(n)))
                print("e           : {}".format(eval(e, model)))
                print("e'          : {}".format(eval(e_prime, model)))
                print("inner       : {}".format(eval(einner, model)))
                print("inner'      : {}".format(eval(mutate(einner, s), model)))
                print("del[inner]  : {}".format(eval(d2, model)))
                print("add[inner]  : {}".format(eval(n2, model)))
                print("del[ireal]  : {}".format(eval(einner - mutate(einner, s), model)))
                print("add[ireal]  : {}".format(eval(mutate(einner, s) - einner, model)))
                print("del         : {}".format(eval(d, model)))
                print("add         : {}".format(eval(n, model)))
                print("got         : {}".format(eval(interp, model)))
                raise AssertionError()
        return res
    return g

class ForkOn(Exception):
    def __init__(self, cond):
        super().__init__("fork on {}".format(pprint(cond)))
        self.cond = cond

def fork(context, assumptions, cond, then_branch, else_branch):
    if isinstance(cond, syntax.EBinOp):
        if cond.op == syntax.BOp.And:
            return fork(context, assumptions, cond.e1,
                fork(context, assumptions, cond.e2, then_branch, else_branch),
                else_branch)
        if cond.op == syntax.BOp.Or:
            return fork(context, assumptions, cond.e1,
                then_branch,
                fork(context, assumptions, cond.e2, then_branch, else_branch))
    if isinstance(cond, syntax.EUnaryOp) and cond.op == syntax.UOp.Not:
        return fork(context, assumptions, cond.e, else_branch, then_branch)
    if isinstance(cond, syntax.ECond):
        return fork(context, assumptions, cond.cond,
            fork(context, assumptions, cond.then_branch, then_branch, else_branch),
            fork(context, assumptions, cond.else_branch, then_branch, else_branch))

    solver = solver_for(context)
    if solver.valid(syntax.EImplies(assumptions, cond)):
        return then_branch
    if solver.valid(syntax.EImplies(assumptions, syntax.ENot(cond))):
        return else_branch
    raise ForkOn(cond)

def resolve_forks(assumptions, func):
    try:
        return func(assumptions)
    except ForkOn as exc:
        branch_cond = exc.cond
        print("*** forking on {}".format(pprint(branch_cond)))
        return cond(branch_cond,
            resolve_forks(syntax.EAll([assumptions,             branch_cond ]), func),
            resolve_forks(syntax.EAll([assumptions, syntax.ENot(branch_cond)]), func))

@dbg
def bag_delta(e, context, s, assumptions : syntax.Exp = syntax.T):
    # print("-" * 20)
    # print("{}.....{}".format(pprint(e), pprint(s)))

    empty = syntax.EEmptyList().with_type(e.type)

    if isinstance(e, target_syntax.EStateVar):
        return checked_bag_delta(e.e, context, s, assumptions)

    if isinstance(e, target_syntax.EMap):
        t = e.type
        e = target_syntax.EFlatMap(e.e, syntax.ELambda(e.f.arg,
            syntax.ESingleton(e.f.body).with_type(t))).with_type(t)

    if isinstance(e, target_syntax.EFilter):
        t = e.type
        e = target_syntax.EFlatMap(e.e, syntax.ELambda(e.p.arg,
            cond(e.p.body,
                syntax.ESingleton(e.p.arg).with_type(t),
                syntax.EEmptyList().with_type(t)))).with_type(t)

    if isinstance(e, target_syntax.EFlatMap):
        arg = fresh_var(e.f.arg.type)
        func_body = e.f.apply_to(arg)

        xs = e.e
        added_xs, removed_xs = checked_bag_delta(xs, context, s, assumptions)
        inner_context = UnderBinder(context, arg, xs, RUNTIME_POOL)
        added_ys = resolve_forks(assumptions,
            lambda a: checked_bag_delta(func_body, inner_context, s, a)[0])
        removed_ys = resolve_forks(assumptions,
            lambda a: checked_bag_delta(func_body, inner_context, s, a)[1])
        # print("D_{{xs}} = {}".format(pprint(removed_xs)))
        # print("A_{{xs}} = {}".format(pprint(added_xs)))
        # print("D_{{f}}  = {}".format(pprint(removed_ys)))
        # print("A_{{f}}  = {}".format(pprint(added_ys)))

        # new_body = better_mutate(statevar(func_body), inner_context, s, assumptions)
        new_body = mutate(func_body, s)

        post_remove = bag_subtract(context, assumptions, xs, removed_xs)
        removed = bag_union(
            flatmap(context, assumptions, removed_xs, e.f),
            flatmap(context, assumptions, post_remove, syntax.ELambda(arg, removed_ys)))
        added = bag_union(
            flatmap(context, assumptions, added_xs, syntax.ELambda(arg, new_body)),
            flatmap(context, assumptions, post_remove, syntax.ELambda(arg, added_ys)))

        if alpha_equivalent(added, removed):
            added = removed = empty

        i = bag_intersection(context, assumptions, removed, added)

        # if alpha_equivalent(added, removed):
        #     return (empty, empty)

        # if valid(syntax.EImplies(assumptions, syntax.EEmpty(i))):
        #     i = empty
        # else:
        #     print("+ {}".format(pprint(added)))
        #     print("- {}".format(pprint(removed)))
        #     raise NotEfficient(i)

        print(" --> + {}".format(pprint(added)))
        print(" --> - {}".format(pprint(removed)))
        print(" --> i {}".format(pprint(i)))
        return (bag_subtract(context, assumptions, added, i), bag_subtract(context, assumptions, removed, i))

    if isinstance(e, syntax.EBinOp) and e.op == "+":
        n1, d1 = checked_bag_delta(e.e1, context, s, assumptions)
        n2, d2 = checked_bag_delta(e.e2, context, s, assumptions)
        return (bag_union(n1, n2), bag_union(d1, d2))

    if isinstance(e, syntax.EBinOp) and e.op == "-":
        # (xs - d1 + n1) - (ys - d2 + n2)
        # assume ys' \subsetof xs
        # assume ys \subsetof xs
        n1, d1 = checked_bag_delta(e.e1, context, s, assumptions)
        n2, d2 = checked_bag_delta(e.e2, context, s, assumptions)
        return (
            bag_union(n1, d2),
            bag_union(d1, n2))

    if isinstance(e, syntax.EUnaryOp) and e.op == syntax.UOp.Distinct:
        n, d = checked_bag_delta(e.e, context, s, assumptions)
        n = edistinct(n)
        d = edistinct(d)
        if not definitely(are_unique(e.e)):
            x = fresh_var(e.type.t)
            d = efilter(edistinct(d), syntax.ELambda(x, equal(target_syntax.ECountIn(x, e.e), syntax.ONE)))
        return (n, d)

    if isinstance(e, syntax.ESingleton):
        new_e = better_mutate(statevar(e.e), context, s, assumptions)
        assert retypecheck(new_e), pprint(e)
        return fork(context, assumptions, syntax.EEq(e.e, new_e),
            (empty, empty),
            (syntax.ESingleton(new_e).with_type(e.type), e))
        # if alpha_equivalent(new_e, e.e):
        #     return (empty, empty)
        # else:
        #     ch = changed(e, context, s, assumptions)
        #     return (
        #         cond(ch, syntax.ESingleton(new_e).with_type(e.type), empty),
        #         cond(ch, e, empty))

    if isinstance(e, syntax.EEmptyList):
        return (empty, empty)

    if isinstance(e, syntax.ECond):
        new_cond = mutate(e.cond, s)
        if alpha_equivalent(new_cond, e.cond):
            n1, d1 = checked_bag_delta(e.then_branch, context, s, assumptions)
            n2, d2 = checked_bag_delta(e.else_branch, context, s, assumptions)
            return (
                cond(e.cond, n1, n2),
                cond(e.cond, d1, d2))
        else:

            # print(" -> {}".format(pprint(e.cond)))
            # print(" becomes T: {}".format(pprint(became_true (e.cond, context, s, assumptions))))
            # print(" becomes F: {}".format(pprint(became_false(e.cond, context, s, assumptions))))

            case1 = cond(became_false(e.cond, context, s, assumptions), bag_subtract(context, assumptions, e.else_branch, e.then_branch), empty)
            case2 = cond(became_true (e.cond, context, s, assumptions), bag_subtract(context, assumptions, e.then_branch, e.else_branch), empty)
            case3 = cond(became_false(e.cond, context, s, assumptions), bag_subtract(context, assumptions, e.then_branch, e.else_branch), empty)
            case4 = cond(became_true (e.cond, context, s, assumptions), bag_subtract(context, assumptions, e.else_branch, e.then_branch), empty)
            added = cond(e.cond, case1, case2)
            removed = cond(e.cond, case3, case4)
            return (added, removed)

    # if isinstance(e, syntax.EVar):
    #     n = d = syntax.EEmptyList().with_type(e.type)
    #     for step in flatten(s):
    #         if isinstance(step, syntax.SCall):
    #             assert isinstance(step.target, syntax.EVar)
    #             if step.target == e:
    #                 if step.func == "add_all":
    #                     n = bag_union(n, step.args[0])
    #                 elif step.func == "remove_all":
    #                     d = bag_union(d, step.args[0])
    #                 else:
    #                     raise ValueError(step.func)
    #         elif isinstance(step, syntax.SAssign) and isinstance(step.lhs, syntax.EVar) and step.lhs != e:
    #             pass
    #         else:
    #             raise NotImplementedError(step)
    #     assert retypecheck(n)
    #     assert retypecheck(d)
    #     assert is_collection(n.type), pprint(n)
    #     assert is_collection(d.type), pprint(d)
    #     return (n, d)

    # raise NotImplementedError(type(e))

    if not isinstance(e, syntax.EVar):
        raise NotImplementedError(e)

    new_e = mutate(e, s)

    if isinstance(new_e, syntax.EBinOp) and new_e.op == "+" and isinstance(new_e.e1, syntax.EBinOp) and new_e.e1.op == "-" and alpha_equivalent(new_e.e1.e1, e):
        return (new_e.e2, new_e.e1.e2)

    if isinstance(new_e, syntax.EBinOp) and new_e.op == "+" and alpha_equivalent(new_e.e1, e):
        return (new_e.e2, empty)

    try:
        return (
            bag_subtract(context, assumptions, new_e, e),
            bag_subtract(context, assumptions, e, new_e))
    except NotEfficient as exc:
        print(pprint(e))
        print(pprint(s))
        raise

    # if isinstance(s, syntax.SCall) and s.target == e:
    #     if s.func == "add_all":
    #         return (s.args[0], empty)
    #     if s.func == "add":
    #         return (syntax.ESingleton(s.args[0]).with_type(e.type), empty)
    #     if s.func == "remove_all":
    #         return (empty, s.args[0])
    #     if s.func == "remove":
    #         return (empty, syntax.ESingleton(s.args[0]).with_type(e.type))
    #     return (empty, empty)

    # if isinstance(s, syntax.SCall) and isinstance(e, syntax.EVar):
    #     return (empty, empty)

    # if isinstance(s, syntax.SSeq):
    #     while isinstance(s.s1, syntax.SSeq):
    #         s = syntax.SSeq(s.s1.s1, syntax.SSeq(s.s1.s2, s.s2))
    #     if isinstance(s.s1, syntax.SDecl):
    #         n, d = checked_bag_delta(e, s.s2)
    #         n = subst(n, { s.s1.id : s.s1.val })
    #         d = subst(d, { s.s1.id : s.s1.val })
    #         return (n, d)
    #     n1, d1 = checked_bag_delta(e, s.s1)
    #     return checked_bag_delta(bag_union(bag_subtract(e, d1), n1), s.s2)
    #     # n2, d2 = checked_bag_delta(e, s.s2)
    #     # return (
    #     #     bag_union(n1, n2),
    #     #     bag_union(d1, d2))

    # if isinstance(s, syntax.SIf):
    #     nt, dt = checked_bag_delta(e, s.then_branch)
    #     ne, de = checked_bag_delta(e, s.else_branch)
    #     return (cond(s.cond, nt, ne), cond(s.cond, dt, de))

    # if alpha_equivalent(e, mutate(e, s)):
    #     return (empty, empty)

    # # if isinstance(s, syntax.SAssign):
    # #     if alpha_equivalent(e, mutate(e, s)):
    # #         return syntax.EEmptyList().with_type(e.type)

    # print(pprint(e))
    # print(pprint(s))
    # raise NotImplementedError()

# def new_elems(e, s):
#     return bag_delta(e, s)[1]

# def del_elems(e, s):
#     return bag_delta(e, s)[0]

# @typechecked
# def better_mutate_statevars(
#         e       : syntax.Exp,
#         context : Context,
#         op      : syntax.Stm) -> syntax.Exp:
#     class V(BottomUpRewriter):
#         def visit_EStateVar(self, e):
#             return better_mutate(e, context, op)
#     return V().visit(e)

def repair_EStateVar(e : syntax.Exp, available_state : [syntax.Exp]) -> syntax.Exp:
    class V(BottomUpRewriter):
        def visit_EStateVar(self, e):
            return e
        def visit_Exp(self, e):
            if any(alpha_equivalent(e, x) for x in available_state):
                return target_syntax.EStateVar(e).with_type(e.type)
            return super().visit_ADT(e)
    return V().visit(strip_EStateVar(e))

def replace_get_value(e : syntax.Exp, ptr : syntax.Exp, new_value : syntax.Exp) -> syntax.Exp:
    """
    Return an expression representing the value of `e` after writing
    `new_value` to `ptr`.

    This amounts to replacing all instances of `_.val` in `e` with

        (_ == ptr) ? (new_value) : (_.val)
    """
    t = ptr.type
    fvs = free_vars(ptr) | free_vars(new_value)
    class V(BottomUpRewriter):
        def visit_ELambda(self, e):
            if e.arg in fvs:
                v = fresh_var(e.arg.type, omit=fvs)
                e = syntax.ELambda(v, e.apply_to(v))
            return syntax.ELambda(e.arg, self.visit(e.body))
        def visit_EGetField(self, e):
            ee = self.visit(e.e)
            res = syntax.EGetField(ee, e.f).with_type(e.type)
            if e.e.type == t and e.f == "val":
                res = syntax.ECond(syntax.EEq(ee, ptr), new_value, res).with_type(e.type)
            return res
    return V().visit(e)

def _do_assignment(lval : syntax.Exp, new_value : syntax.Exp, e : syntax.Exp) -> syntax.Exp:
    """
    Return the value of `e` after the assignment `lval = new_value`.
    """
    if isinstance(lval, syntax.EVar):
        return subst(e, { lval.id : new_value })
    elif isinstance(lval, syntax.EGetField):
        if isinstance(lval.e.type, syntax.THandle):
            assert lval.f == "val"
            # Because any two handles might alias, we need to rewrite all
            # reachable handles in `e`.
            return replace_get_value(e, lval.e, new_value)
        return _do_assignment(lval.e, _replace_field(lval.e, lval.f, new_value), e)
    else:
        raise Exception("not an lvalue: {}".format(pprint(lval)))

def _replace_field(record : syntax.Exp, field : str, new_value : syntax.Exp) -> syntax.Exp:
    return syntax.EMakeRecord(tuple(
        (f, new_value if f == field else syntax.EGetField(record, f).with_type(ft))
        for f, ft in record.type.fields)).with_type(record.type)

def pprint_value(ty, val):
    if isinstance(ty, syntax.TBag) or isinstance(ty, syntax.TSet):
        if not val: return "{}"
        if len(val) == 1: return "{{{}}}".format(pprint_value(ty.t, val[0]))
        return "{{\n    {}}}".format(",\n    ".join(pprint_value(ty.t, v) for v in val))
    if isinstance(ty, syntax.TList):
        if not val: return "[]"
        if len(val) == 1: return "[{}]".format(pprint_value(ty.t, val[0]))
        return "[\n    {}]".format(",\n    ".join(pprint_value(ty.t, v) for v in val))
    if isinstance(ty, syntax.TRecord):
        return "{{{}}}".format(", ".join("{}: {}".format(f, val[f]) for f, ft in sorted(ty.fields)))
    if isinstance(ty, syntax.TNative):
        return "${}".format(val[1])
    if isinstance(ty, target_syntax.TMap):
        return "{{{}}}".format(", ".join("{} -> {}".format(*i) for i in val.items()))
    return repr(val)

def pprint_model(model, env):
    for var_id, val in sorted(model.items()):
        if var_id not in env:
            print("  {} = {!r}".format(var_id, val))
            continue
        ty = env[var_id]
        print("  {} = {}".format(var_id, pprint_value(ty, val)))

def minimal_model(formula, collection_depth=4, solver=None):
    if solver is None:
        from cozy.solver import IncrementalSolver
        solver = IncrementalSolver(collection_depth=collection_depth)
    if solver.satisfiable(formula):
        print("Minimizing model...")
        from cozy.typecheck import is_collection
        collections = [v for v in free_vars(formula) if is_collection(v.type)]
        for max_len in range(collection_depth * len(collections) + 1):
            model = solver.satisfy(syntax.EAll([
                syntax.ELt(syntax.ESum([syntax.ELen(v) for v in collections]), syntax.ENum(max_len).with_type(syntax.INT)),
                formula]))
            if model is not None:
                return model
    return None

def mutate_in_place(
        lval           : syntax.Exp,
        e              : syntax.Exp,
        op             : syntax.Stm,
        abstract_state : [syntax.EVar],
        assumptions    : [syntax.Exp] = None,
        invariants     : [syntax.Exp] = None,
        subgoals_out   : [syntax.Query] = None):

    if False:
        if assumptions is None:
            assumptions = []

        if subgoals_out is None:
            subgoals_out = []

        parts = []

        for stm in flatten(op):
            parts.extend(break_seq(_mutate_in_place(
                lval, e, stm, abstract_state, assumptions, invariants, subgoals_out)))

        return syntax.seq(parts)

    return _mutate_in_place(
        lval, e, op, abstract_state, assumptions, invariants, subgoals_out)

def _mutate_in_place(
        lval           : syntax.Exp,
        e              : syntax.Exp,
        op             : syntax.Stm,
        abstract_state : [syntax.EVar],
        assumptions    : [syntax.Exp] = None,
        invariants     : [syntax.Exp] = None,
        subgoals_out   : [syntax.Query] = None) -> syntax.Stm:
    """
    Produce code to update `lval` that tracks derived value `e` when `op` is
    run.
    """

    if assumptions is None:
        assumptions = []

    if invariants is None:
        invariants = []

    if subgoals_out is None:
        subgoals_out = []

    def make_subgoal(e, a=[], docstring=None):
        if skip_stateless_synthesis.value and not any(v in abstract_state for v in free_vars(e)):
            return e
        query_name = fresh_name("query")
        query = syntax.Query(query_name, syntax.Visibility.Internal, [], assumptions + a, e, docstring)
        query_vars = [v for v in free_vars(query) if v not in abstract_state]
        query.args = [(arg.id, arg.type) for arg in query_vars]
        subgoals_out.append(query)
        return syntax.ECall(query_name, tuple(query_vars)).with_type(e.type)

    h = extension_handler(type(lval.type))
    if h is not None:
        return h.mutate_in_place(
            lval=lval,
            e=e,
            op=op,
            assumptions=assumptions,
            invariants=invariants,
            make_subgoal=make_subgoal)

    # fallback: use an update sketch
    new_e = mutate(e, op)

    if False:
        vars = free_vars(e) | free_vars(op)
        from cozy.contexts import RootCtx
        from cozy.common import partition
        state_vars, args = partition(vars, lambda v: v in abstract_state)
        context = RootCtx(state_vars=state_vars, args=args)
        new_e_prime = better_mutate(target_syntax.EStateVar(e).with_type(e.type), context, op, assumptions=syntax.EAll(assumptions))
        model = minimal_model(syntax.EAll(assumptions + [syntax.ENot(syntax.EEq(new_e, new_e_prime))]))
        if model is not None:
            from cozy.evaluation import eval
            from cozy.contexts import shred
            from cozy.common import unique

            print(pprint(op))
            pprint_model(model, { v.id : v.type for v in (free_vars(e) | free_vars(syntax.EAll(assumptions))) })
            print("--->")
            for v in (free_vars(e) | free_vars(syntax.EAll(assumptions))):
                print("  {} = {}".format(v.id, pprint_value(v.type, eval(mutate(v, op), model))))

            print(pprint(new_e))
            print(pprint(new_e_prime))

            print("expected: {}".format(eval(new_e, model)))
            print("got: {}".format(eval(new_e_prime, model)))

            for x, x_ctx, _ in unique(shred(new_e_prime, context)):
                if x_ctx == context:
                    print("=" * 20)
                    print("{} ----> {}".format(pprint(x), pprint_value(x.type, eval(x, model))))

            raise Exception("wtf")

        from cozy.cost_model import asymptotic_runtime, is_constant_time
        print("asymptotic cost: {}".format(asymptotic_runtime(new_e_prime)))
        if not is_constant_time(new_e_prime):
            raise NotEfficient(new_e_prime)

        return syntax.SAssign(lval, make_subgoal(new_e_prime))

    s, sgs = sketch_update(lval, e, new_e, ctx=abstract_state, assumptions=assumptions, invariants=invariants)
    subgoals_out.extend(sgs)
    return s

def value_at(m, k):
    """Make an AST node for m[k]."""
    if isinstance(m, target_syntax.EMakeMap2):
        return syntax.ECond(
            syntax.EIn(k, m.e),
            m.value.apply_to(k),
            construct_value(m.type.v)).with_type(m.type.v)
    if isinstance(m, syntax.ECond):
        return syntax.ECond(
            m.cond,
            value_at(m.then_branch, k),
            value_at(m.else_branch, k)).with_type(m.type.v)
    return target_syntax.EMapGet(m, k).with_type(m.type.v)

def sketch_update(
        lval        : syntax.Exp,
        old_value   : syntax.Exp,
        new_value   : syntax.Exp,
        ctx         : [syntax.EVar],
        assumptions : [syntax.Exp] = [],
        invariants  : [syntax.Exp] = []) -> (syntax.Stm, [syntax.Query]):
    """
    Write code to update `lval` when it changes from `old_value` to `new_value`.
    Variables in `ctx` are assumed to be part of the data structure abstract
    state, and `assumptions` will be appended to all generated subgoals.

    This function returns a statement (code to update `lval`) and a list of
    subgoals (new queries that appear in the code).
    """

    if valid(syntax.EImplies(
            syntax.EAll(itertools.chain(assumptions, invariants)),
            syntax.EEq(old_value, new_value))):
        return (syntax.SNoOp(), [])

    subgoals = []
    new_value = strip_EStateVar(new_value)

    def make_subgoal(e, a=[], docstring=None):
        if skip_stateless_synthesis.value and not any(v in ctx for v in free_vars(e)):
            return e
        query_name = fresh_name("query")
        query = syntax.Query(query_name, syntax.Visibility.Internal, [], assumptions + a, e, docstring)
        query_vars = [v for v in free_vars(query) if v not in ctx]
        query.args = [(arg.id, arg.type) for arg in query_vars]
        subgoals.append(query)
        return syntax.ECall(query_name, tuple(query_vars)).with_type(e.type)

    def recurse(*args, **kwargs):
        (code, sgs) = sketch_update(*args, **kwargs)
        subgoals.extend(sgs)
        return code

    t = lval.type
    if isinstance(t, syntax.TBag) or isinstance(t, syntax.TSet):
        to_add = make_subgoal(syntax.EBinOp(new_value, "-", old_value).with_type(t), docstring="additions to {}".format(pprint(lval)))
        to_del = make_subgoal(syntax.EBinOp(old_value, "-", new_value).with_type(t), docstring="deletions from {}".format(pprint(lval)))
        v = fresh_var(t.t)
        stm = syntax.seq([
            syntax.SForEach(v, to_del, syntax.SCall(lval, "remove", [v])),
            syntax.SForEach(v, to_add, syntax.SCall(lval, "add", [v]))])
    elif is_numeric(t) and update_numbers_with_deltas.value:
        change = make_subgoal(syntax.EBinOp(new_value, "-", old_value).with_type(t), docstring="delta for {}".format(pprint(lval)))
        stm = syntax.SAssign(lval, syntax.EBinOp(lval, "+", change).with_type(t))
    elif isinstance(t, syntax.TTuple):
        get = lambda val, i: syntax.ETupleGet(val, i).with_type(t.ts[i])
        stm = syntax.seq([
            recurse(get(lval, i), get(old_value, i), get(new_value, i), ctx, assumptions,
                invariants=invariants)
            for i in range(len(t.ts))])
    elif isinstance(t, syntax.TRecord):
        get = lambda val, i: syntax.EGetField(val, t.fields[i][0]).with_type(t.fields[i][1])
        stm = syntax.seq([
            recurse(get(lval, i), get(old_value, i), get(new_value, i), ctx, assumptions,
                invariants=invariants)
            for i in range(len(t.fields))])
    elif isinstance(t, syntax.TMap):
        k = fresh_var(lval.type.k)
        v = fresh_var(lval.type.v)
        key_bag = syntax.TBag(lval.type.k)

        old_keys = target_syntax.EMapKeys(old_value).with_type(key_bag)
        new_keys = target_syntax.EMapKeys(new_value).with_type(key_bag)

        # (1) exit set
        deleted_keys = syntax.EBinOp(old_keys, "-", new_keys).with_type(key_bag)
        s1 = syntax.SForEach(k, make_subgoal(deleted_keys, docstring="keys removed from {}".format(pprint(lval))),
            target_syntax.SMapDel(lval, k))

        # (2) enter/mod set
        new_or_modified = target_syntax.EFilter(new_keys,
            syntax.ELambda(k, syntax.EAny([syntax.ENot(syntax.EIn(k, old_keys)), syntax.ENot(syntax.EEq(value_at(old_value, k), value_at(new_value, k)))]))).with_type(key_bag)
        update_value = recurse(
            v,
            value_at(old_value, k),
            value_at(new_value, k),
            ctx = ctx,
            assumptions = assumptions + [syntax.EIn(k, new_or_modified), syntax.EEq(v, value_at(old_value, k))],
            invariants = invariants)
        s2 = syntax.SForEach(k, make_subgoal(new_or_modified, docstring="new or modified keys from {}".format(pprint(lval))),
            target_syntax.SMapUpdate(lval, k, v, update_value))

        stm = syntax.SSeq(s1, s2)
    else:
        # Fallback rule: just compute a new value from scratch
        stm = syntax.SAssign(lval, make_subgoal(new_value, docstring="new value for {}".format(pprint(lval))))

    return (stm, subgoals)


_SOLVERS = { }
def solver_for(context):
    s = _SOLVERS.get(context)
    if s is None:
        s = ModelCachingSolver(
            vars=[v for v, p in context.vars()],
            funcs=context.funcs())
        _SOLVERS[context] = s
    return s

def optimize_lambda(f, bag, context, pc):
    context = UnderBinder(context, f.arg, bag, RUNTIME_POOL)
    return syntax.ELambda(f.arg,
        optimize(f.body, context, syntax.EAll([pc, syntax.EIn(f.arg, bag)])))

def optimize(e, context, pc = syntax.T):

    if isinstance(e, int) or isinstance(e, float) or isinstance(e, str):
        return e

    if isinstance(e, tuple):
        return tuple(optimize(x, context, pc) for x in e)

    if isinstance(e, target_syntax.EStateVar):
        return e

    solver = solver_for(context)
    if e.type == syntax.BOOL:
        if solver.valid(syntax.EImplies(pc, e)):
            return syntax.T
        if solver.valid(syntax.EImplies(pc, syntax.ENot(e))):
            return syntax.F
    if is_collection(e.type):
        if solver.valid(syntax.EImplies(pc, syntax.EEmpty(e))):
            return syntax.EEmptyList().with_type(e.type)

    if not isinstance(e, syntax.Exp):
        raise NotImplementedError(repr(e))

    # (1) recurse
    if isinstance(e, target_syntax.EFilter):
        new_children = [
            optimize(e.e, context, pc),
            optimize_lambda(e.p, e.e, context, pc)]
    elif isinstance(e, target_syntax.EMap) or isinstance(e, target_syntax.EFlatMap) or isinstance(e, syntax.EArgMin) or isinstance(e, syntax.EArgMax):
        new_children = [
            optimize(e.e, context, pc),
            optimize_lambda(e.f, e.e, context, pc)]
    elif isinstance(e, syntax.ECond):
        new_children = [
            optimize(e.cond, context, pc),
            optimize(e.then_branch, context, syntax.EAll([pc, e.cond])),
            optimize(e.then_branch, context, syntax.EAll([pc, syntax.ENot(e.cond)]))]
    elif isinstance(e, syntax.EBinOp) and e.op == syntax.BOp.And:
        new_children = [
            optimize(e.e1, context, pc),
            e.op,
            optimize(e.e2, context, syntax.EAll([pc, e.e1]))]
    elif isinstance(e, syntax.EBinOp) and e.op == syntax.BOp.Or:
        new_children = [
            optimize(e.e1, context, pc),
            e.op,
            optimize(e.e2, context, syntax.EAll([pc, syntax.ENot(e.e1)]))]
    else:
        assert not any(isinstance(x, target_syntax.ELambda) for x in e.children()), pprint(e)
        new_children = [optimize(c, context, pc) for c in e.children()]

    # (2) optimize
    if isinstance(e, target_syntax.EMap):
        bag, func = new_children
        if func.arg == func.body:
            return bag
        if isinstance(bag, syntax.ESingleton):
            return syntax.ESingleton(optimize(func.apply_to(bag.e), context, pc)).with_type(e.type)
        if isinstance(bag, syntax.EBinOp) and bag.op == "+":
            return optimize(syntax.EBinOp(
                target_syntax.EMap(bag.e1, func).with_type(e.type), bag.op,
                target_syntax.EMap(bag.e2, func).with_type(e.type)).with_type(e.type), context, pc)
        if isinstance(bag, syntax.EBinOp) and bag.op == "-" and solver.valid(target_syntax.EIsSubset(bag.e2, bag.e1)):
            return optimize(syntax.EBinOp(
                target_syntax.EMap(bag.e1, func).with_type(e.type), bag.op,
                target_syntax.EMap(bag.e2, func).with_type(e.type)).with_type(e.type), context, pc)
    if isinstance(e, target_syntax.EFilter):
        bag, pred = new_children
        if e.p.body == syntax.T:
            return bag
        if e.p.body == syntax.F:
            return syntax.EEmptyList().with_type(e.type)
        if isinstance(bag, syntax.EBinOp) and bag.op in ("+", "-"):
            return optimize(syntax.EBinOp(
                target_syntax.EFilter(bag.e1, pred).with_type(e.type), bag.op,
                target_syntax.EFilter(bag.e2, pred).with_type(e.type)).with_type(e.type), context, pc)
        if isinstance(bag, syntax.ESingleton):
            return cond(
                optimize(pred.apply_to(bag.e), context, pc),
                bag,
                syntax.EEmptyList().with_type(e.type))
    if isinstance(e, syntax.EBinOp):
        if e.op == syntax.BOp.And:
            return syntax.EAll([new_children[0], new_children[2]])
        if e.op == syntax.BOp.Or:
            return syntax.EAny([new_children[0], new_children[2]])
    if isinstance(e, syntax.EGetField):
        record, field_name = new_children
        if isinstance(record, syntax.EMakeRecord):
            return dict(record.fields)[field_name]
    if isinstance(e, syntax.EBinOp) and e.op == "+":
        e1, _, e2 = new_children
        if isinstance(e1, syntax.EEmptyList) or e1 == syntax.ZERO:
            return e2
        if isinstance(e2, syntax.EEmptyList) or e2 == syntax.ZERO:
            return e1

    return type(e)(*new_children).with_type(e.type)

def real_better_mutate(e, context, op, assumptions=syntax.T):
    return resolve_forks(assumptions,
        lambda a: better_mutate(
            e=e,
            context=context,
            op=op,
            assumptions=a))

def test():

    from cozy.parse import parse_exp, parse_stm
    from cozy.desugar import desugar_list_comprehensions
    from cozy.contexts import RootCtx
    from cozy.syntax import EVar, INT, INT_BAG, TFunc

    context = RootCtx(
        state_vars=[EVar("xs").with_type(INT_BAG)],
        args=[EVar("arg").with_type(INT), EVar("arg2").with_type(INT)],
        funcs={
            "f": TFunc((INT,), INT),
            "g": TFunc((INT,), INT),
        })

    type_env = { v.id : v.type for v, p in context.vars() }

    def read_exp(s):
        e = parse_exp(s)
        assert retypecheck(e, env=type_env, fenv=context.funcs())
        return desugar_list_comprehensions(e)

    def read_stm(s):
        s = parse_stm(s)
        assert retypecheck(s, env=type_env, fenv=context.funcs())
        return desugar_list_comprehensions(s)

        e = to_bag(read_exp("[ x | x <- xs, x == 0 ]"))
        s = read_stm("xs.add(arg);")

    cases = [
        # (to_bag(read_exp("[ x | x <- xs, x != 0 ]")),
        #     read_stm("xs.add(arg);")),
        # (read_exp("min [ x | x <- xs, x != 0 ]"),
        #     read_stm("xs.add(arg);")),
        # (read_exp("min xs"),
        #     read_stm("xs.remove(arg);")),
        # (read_exp("min [ x | x <- xs, x != 0 ]"),
        #     read_stm("xs.remove(arg);")),
        # (read_exp("min [ x | x <- xs, x != 0 ]"),
        #     read_stm("xs.remove(arg); xs.add(arg2);")),
        # (read_exp("min [ x | x <- xs, g(x) == arg2 ]"),
        #     read_stm("xs.remove(g(arg));")),
        (read_exp("min [ max [f(y) | y <- xs, g(y) == g(x)] | x <- xs ]"),
            read_stm("xs.remove(arg); xs.add(arg2);")),
        ]

    reses = []
    for e, s in cases:
        reses.append(real_better_mutate(
            e=statevar(e),
            context=context,
            op=s))

    print("=" * 40 + " report")
    for i, ((e, s), res) in enumerate(zip(cases, reses)):
        print("-" * 20 + " case {}/{}".format(i+1, len(cases)))
        print(pprint(e))
        print(pprint(s))
        print(pprint(res))

    import sys
    sys.exit(0)

def run():

    from cozy.syntax import EVar, SNoOp, TNative, TEnum, EGetField, TRecord, TInt, ELambda, EBinOp, TBool, TBag, SAssign, SSeq, SIf, TMap, SCall, ESingleton, EMakeRecord, TList, EUnaryOp, ECall, EArgMin, EArgMax, ENum, SDecl, EEnumEntry, EAll, TSet, TFunc
    from cozy.target_syntax import EMap, EFilter, EMakeMap2, EStateVar
    from cozy.contexts import RootCtx
    from cozy.pools import RUNTIME_POOL

    # lval, e, s = (EVar('_var102529').with_type(TMap(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'), TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))), EMakeMap2(EMap(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var98754').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('_var98754').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')))).with_type(TBag(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))), ELambda(EVar('_key98752').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), '==', EVar('_key98752').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))))).with_type(TMap(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'), TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))), SSeq(SDecl('c', EUnaryOp('the', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), '==', EVar('i').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), SSeq(SCall(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), 'remove_all', (EMap(EFilter(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), ELambda(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), EBinOp(EGetField(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), 'host_id').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()))).with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), ELambda(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))))).with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))),)), SSeq(SCall(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), 'remove', (EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))),)), SSeq(SAssign(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), EBinOp(ESingleton(EMakeRecord((('conn_state', EEnumEntry('READY').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))), ('conn_host', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))), ('conn_iface', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))), ('conn_next_refresh', ECall('after', (EVar('lastUsed').with_type(TNative('mongo::Date_t')), EVar('refreshRequirement').with_type(TNative('mongo::Milliseconds')))).with_type(TNative('mongo::Date_t'))), ('conn_returned', EVar('now').with_type(TNative('mongo::Date_t'))), ('conn_last_used', EVar('retId').with_type(TInt())), ('conn_dropped', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_dropped').with_type(TBool())))).with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), '+', EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))), SSeq(SIf(EUnaryOp('not', EBinOp(EUnaryOp('exists', EMap(EFilter(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EBinOp(EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_host').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()))).with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TBool()), 'or', EUnaryOp('exists', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var3771').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('_var3771').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('_var3771').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var3772').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('_var3772').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBool())).with_type(TBool())).with_type(TBool()), SCall(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), 'add', (EMakeRecord((('host_id', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))), ('host_timeout', ECall('after', (EArgMax(EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var3773').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('_var3773').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('_var3773').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '!=', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var3774').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('_var3774').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_returned').with_type(TNative('mongo::Date_t')))).with_type(TBag(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t')), EVar('hostTimeout').with_type(TNative('mongo::Milliseconds')))).with_type(TNative('mongo::Date_t'))))).with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))),)), SNoOp()), SAssign(EVar('retId').with_type(TInt()), EBinOp(EVar('retId').with_type(TInt()), '+', ENum(1).with_type(TInt())).with_type(TInt()))))))))

    lval = EVar('_var22838').with_type(TNative('mongo::Date_t'))

    # _idlehosts bar nonsense
    # e = EArgMin(EMap(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), ELambda(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), EGetField(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), 'host_timeout').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t'))

    # nextEvent
    # e = EArgMin(EBinOp(EBinOp(EMap(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_expiration').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), '+', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('READY').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_next_refresh').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), '+', EMap(EMap(EFilter(EUnaryOp('distinct', EMap(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort'))), ELambda(EVar('p').with_type(TNative('mongo::HostAndPort')), EUnaryOp('not', EBinOp(EUnaryOp('exists', EMap(EFilter(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EBinOp(EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_host').with_type(TNative('mongo::HostAndPort')), '==', EVar('p').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()))).with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TList(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TBool()), 'or', EUnaryOp('exists', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EVar('p').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBool())).with_type(TBool())).with_type(TBool()))).with_type(TList(TNative('mongo::HostAndPort'))), ELambda(EVar('p').with_type(TNative('mongo::HostAndPort')), EVar('p').with_type(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort'))), ELambda(EVar('p').with_type(TNative('mongo::HostAndPort')), ECall('after', (EArgMax(EMap(EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EVar('p').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '!=', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_returned').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t')), EVar('hostTimeout').with_type(TNative('mongo::Milliseconds')))).with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t'))

    # nextEvent using _idleHosts
    # e = EArgMin(EBinOp(EBinOp(EMap(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_expiration').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), '+', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('READY').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_next_refresh').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), '+', EMap(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), ELambda(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), EGetField(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), 'host_timeout').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t'))

    # nextEvent computing idleHosts by subtraction
    e = EArgMin(EBinOp(EBinOp(EMap(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_expiration').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), '+', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('READY').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_next_refresh').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), '+', EMap(EBinOp(EUnaryOp('distinct', EMap(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort'))), '-', EUnaryOp('distinct', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool()), 'or', EUnaryOp('exists', EMap(EFilter(EVar('reqs').with_type(TSet(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EBinOp(EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_host').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()))).with_type(TSet(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TList(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort'))), ELambda(EVar('p').with_type(TNative('mongo::HostAndPort')), ECall('after', (EArgMax(EMap(EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EVar('p').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '!=', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_returned').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t')), EVar('hostTimeout').with_type(TNative('mongo::Milliseconds')))).with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t'))

    s = SSeq(SDecl('c', EUnaryOp('the', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), '==', EVar('i').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), SSeq(SCall(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), 'remove_all', (EMap(EFilter(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), ELambda(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), EBinOp(EGetField(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), 'host_id').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()))).with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), ELambda(EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))), EVar('h').with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))))).with_type(TList(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))),)), SSeq(SCall(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), 'remove', (EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))),)), SSeq(SCall(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), 'add', (EMakeRecord((('conn_state', EEnumEntry('READY').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))), ('conn_host', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))), ('conn_iface', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))), ('conn_next_refresh', ECall('after', (EVar('lastUsed').with_type(TNative('mongo::Date_t')), EVar('refreshRequirement').with_type(TNative('mongo::Milliseconds')))).with_type(TNative('mongo::Date_t'))), ('conn_returned', EVar('now').with_type(TNative('mongo::Date_t'))), ('conn_last_used', EVar('retId').with_type(TInt())), ('conn_dropped', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_dropped').with_type(TBool())))).with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))),)), SSeq(SIf(EUnaryOp('not', EBinOp(EUnaryOp('exists', EMap(EFilter(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EBinOp(EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_host').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()))).with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TList(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TBool()), 'or', EUnaryOp('exists', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var6546').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('_var6546').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('_var6546').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var6547').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('_var6547').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBool())).with_type(TBool())).with_type(TBool()), SCall(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), 'add', (EMakeRecord((('host_id', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))), ('host_timeout', ECall('after', (EArgMax(EMap(EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var6549').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('_var6549').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('_var6549').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '!=', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var6550').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('_var6550').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('_var6548').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('_var6548').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_returned').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t')), EVar('hostTimeout').with_type(TNative('mongo::Milliseconds')))).with_type(TNative('mongo::Date_t'))))).with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))),)), SNoOp()), SAssign(EVar('retId').with_type(TInt()), EBinOp(EVar('retId').with_type(TInt()), '+', ENum(1).with_type(TInt())).with_type(TInt())))))))
    assumptions = [
        EUnaryOp('unique', EMap(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')))).with_type(TList(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')))).with_type(TBool()),
        EUnaryOp('all', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('READY').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_last_used').with_type(TInt()), '<', EVar('retId').with_type(TInt())).with_type(TBool()))).with_type(TList(TBool()))).with_type(TBool()),
        EUnaryOp('unique', EMap(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_callback').with_type(TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')))).with_type(TList(TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')))).with_type(TBool()),
        EBinOp(EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))), '==', EMap(EMap(EFilter(EUnaryOp('distinct', EMap(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort'))), ELambda(EVar('p').with_type(TNative('mongo::HostAndPort')), EUnaryOp('not', EBinOp(EUnaryOp('exists', EMap(EFilter(EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EBinOp(EGetField(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), 'rq_host').with_type(TNative('mongo::HostAndPort')), '==', EVar('p').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()))).with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))), ELambda(EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))), EVar('r').with_type(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TList(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TBool()), 'or', EUnaryOp('exists', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EVar('p').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '==', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBool())).with_type(TBool())).with_type(TBool()))).with_type(TList(TNative('mongo::HostAndPort'))), ELambda(EVar('p').with_type(TNative('mongo::HostAndPort')), EVar('p').with_type(TNative('mongo::HostAndPort')))).with_type(TList(TNative('mongo::HostAndPort'))), ELambda(EVar('p').with_type(TNative('mongo::HostAndPort')), EMakeRecord((('host_id', EVar('p').with_type(TNative('mongo::HostAndPort'))), ('host_timeout', ECall('after', (EArgMax(EMap(EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_host').with_type(TNative('mongo::HostAndPort')), '==', EVar('p').with_type(TNative('mongo::HostAndPort'))).with_type(TBool()), 'and', EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '!=', EEnumEntry('CHECKED_OUT').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool())).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_returned').with_type(TNative('mongo::Date_t')))).with_type(TList(TNative('mongo::Date_t'))), ELambda(EVar('x').with_type(TNative('mongo::Date_t')), EVar('x').with_type(TNative('mongo::Date_t')))).with_type(TNative('mongo::Date_t')), EVar('hostTimeout').with_type(TNative('mongo::Milliseconds')))).with_type(TNative('mongo::Date_t'))))).with_type(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))))).with_type(TList(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))))).with_type(TBool()),
        EUnaryOp('unique', EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBool()),
        EUnaryOp('unique', EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort'))))))).with_type(TBool()),
        EUnaryOp('unique', EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t'))))))).with_type(TBool()),

        EUnaryOp('exists', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), '==', EVar('i').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TBool()),
        EBinOp(EGetField(EUnaryOp('the', EMap(EFilter(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_iface').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), '==', EVar('i').with_type(TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))).with_type(TBool()))).with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TList(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))))).with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_state').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), '!=', EEnumEntry('READY').with_type(TEnum(('READY', 'PROCESSING', 'CHECKED_OUT')))).with_type(TBool()),
        EUnaryOp('all', EMap(EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))), ELambda(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), EBinOp(EVar('now').with_type(TNative('mongo::Date_t')), '>=', EGetField(EVar('c').with_type(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool())))), 'conn_returned').with_type(TNative('mongo::Date_t'))).with_type(TBool()))).with_type(TList(TBool()))).with_type(TBool())]

    abstate = [
            EVar('minConnections').with_type(TInt()),
            EVar('maxConnections').with_type(TInt()),
            EVar('maxConnecting').with_type(TInt()),
            EVar('refreshTimeout').with_type(TNative('mongo::Milliseconds')),
            EVar('refreshRequirement').with_type(TNative('mongo::Milliseconds')),
            EVar('hostTimeout').with_type(TNative('mongo::Milliseconds')),
            EVar('conns').with_type(TBag(TRecord((('conn_state', TEnum(('READY', 'PROCESSING', 'CHECKED_OUT'))), ('conn_host', TNative('mongo::HostAndPort')), ('conn_iface', TNative('mongo::executor::ConnectionPool::ConnectionInterface*')), ('conn_next_refresh', TNative('mongo::Date_t')), ('conn_returned', TNative('mongo::Date_t')), ('conn_last_used', TInt()), ('conn_dropped', TBool()))))),
            EVar('reqs').with_type(TBag(TRecord((('rq_callback', TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')), ('rq_expiration', TNative('mongo::Date_t')), ('rq_host', TNative('mongo::HostAndPort')))))),
            EVar('_idleHosts').with_type(TBag(TRecord((('host_id', TNative('mongo::HostAndPort')), ('host_timeout', TNative('mongo::Date_t')))))),
            EVar('retId').with_type(TInt())]

    context = RootCtx(
        state_vars=abstate,
        args=[v for v in (free_vars(e) | free_vars(s)) if v not in abstate],
        funcs=OrderedDict([('eternity', TFunc((), TNative('mongo::Date_t'))),
             ('after',
              TFunc((TNative('mongo::Date_t'), TNative('mongo::Milliseconds')), TNative('mongo::Date_t'))),
             ('nullConn',
              TFunc((), TNative('mongo::executor::ConnectionPool::ConnectionInterface*'))),
             ('nullReq',
              TFunc((), TNative('mongo::executor::ConnectionPool::GetConnectionCallback*')))]))

    sqs = []

    print(pprint(e))
    print("-"*40)
    print("// IN: {}".format(", ".join(v.id for v, p in context.vars() if p == RUNTIME_POOL)))
    print(pprint(s))
    print("-"*40)

    print(pprint(mutate(e, s)))

    print("-"*40)

    from cozy.syntax_tools import cse
    from cozy.solver import break_let

    print("Mutating...")
    # e_prime = better_mutate(
    #     e=EStateVar(e).with_type(e.type),
    #     context=context,
    #     op=s,
    #     assumptions=EAll(assumptions))
    e_prime = real_better_mutate(
        e=EStateVar(e).with_type(e.type),
        context=context,
        op=s,
        assumptions=syntax.EAll(assumptions))

    print("Optimizing...")
    e_prime_opt = optimize(e_prime, context=context, pc=EAll(assumptions))

    print("Eliminating common subexpressions...")
    e_prime_opt_cse = cse(e_prime)

    for part in break_let(e_prime_opt_cse):
        if isinstance(part, syntax.Exp):
            print("return {}".format(pprint(part)))
        else:
            var, val = part
            print("{} = {}".format(var.id, pprint(val)))

    # print(pprint(better_mutate(
    #     e=EStateVar(e).with_type(e.type),
    #     context=context,
    #     op=s,
    #     assumptions=EAll(assumptions))))

    return

    print(pprint(mutate_in_place(lval, e, s, abstate, subgoals_out=sqs, assumptions=assumptions)))

    for q in sqs:
        print("-"*40)
        print(pprint(q))

        from cozy.syntax_tools import unpack_representation

        # return

        ret = optimize_to_fixpoint(
            repair_EStateVar(q.ret, abstate),
            context=context,
            validate=True,
            assumptions=EAll(assumptions))
        rep, ret = unpack_representation(ret)
        print(" ---> {}".format(pprint(ret)))

if __name__ == "__main__":
    test()
    run()
