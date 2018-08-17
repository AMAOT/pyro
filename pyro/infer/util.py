from __future__ import absolute_import, division, print_function

import math
import numbers
from collections import defaultdict

import torch
from torch.distributions.utils import broadcast_all

from pyro.distributions.util import is_identically_zero
from pyro.ops._einsum import shared_intermediates
from pyro.ops.sumproduct import sumproduct
from pyro.poutine.util import site_is_subsample

_VALIDATION_ENABLED = False


def enable_validation(is_validate):
    global _VALIDATION_ENABLED
    _VALIDATION_ENABLED = is_validate


def is_validation_enabled():
    return _VALIDATION_ENABLED


def torch_item(x):
    """
    Like ``x.item()`` for a :class:`~torch.Tensor`, but also works with numbers.
    """
    return x if isinstance(x, numbers.Number) else x.item()


def torch_backward(x):
    """
    Like ``x.backward()`` for a :class:`~torch.Tensor`, but also accepts
    numbers (a no-op if given a number).
    """
    if torch.is_tensor(x):
        x.backward()


def torch_exp(x):
    """
    Like ``x.exp()`` for a :class:`~torch.Tensor`, but also accepts
    numbers.
    """
    if torch.is_tensor(x):
        return torch.exp(x)
    else:
        return math.exp(x)


def detach_iterable(iterable):
    if torch.is_tensor(iterable):
        return iterable.detach()
    else:
        return [var.detach() for var in iterable]


def zero_grads(tensors):
    """
    Sets gradients of list of Tensors to zero in place
    """
    for p in tensors:
        if p.grad is not None:
            p.grad = p.grad.new_zeros(p.shape)


def get_iarange_stacks(trace):
    """
    This builds a dict mapping site name to a set of iarange stacks.  Each
    iarange stack is a list of :class:`CondIndepStackFrame`s corresponding to
    an :class:`iarange`.  This information is used by :class:`Trace_ELBO` and
    :class:`TraceGraph_ELBO`.
    """
    return {name: [f for f in node["cond_indep_stack"] if f.vectorized]
            for name, node in trace.nodes.items()
            if node["type"] == "sample" and not site_is_subsample(node)}


class MultiFrameTensor(dict):
    """
    A container for sums of Tensors among different :class:`iarange` contexts.

    Used in :class:`~pyro.infer.tracegraph_elbo.TraceGraph_ELBO` to simplify
    downstream cost computation logic.

    Example::

        downstream_cost = MultiFrameTensor()
        for site in downstream_nodes:
            downstream_cost.add((site["cond_indep_stack"], site["log_prob"]))
        downstream_cost.add(*other_costs.items())  # add in bulk
        summed = downstream_cost.sum_to(target_site["cond_indep_stack"])
    """
    def __init__(self, *items):
        super(MultiFrameTensor, self).__init__()
        self.add(*items)

    def add(self, *items):
        """
        Add a collection of (cond_indep_stack, tensor) pairs. Keys are
        ``cond_indep_stack``s, i.e. tuples of :class:`CondIndepStackFrame`s.
        Values are :class:`torch.Tensor`s.
        """
        for cond_indep_stack, value in items:
            frames = frozenset(f for f in cond_indep_stack if f.vectorized)
            assert all(f.dim < 0 and -len(value.shape) <= f.dim for f in frames)
            if frames in self:
                self[frames] = self[frames] + value
            else:
                self[frames] = value

    def sum_to(self, target_frames):
        total = None
        for frames, value in self.items():
            for f in frames:
                if f not in target_frames and value.shape[f.dim] != 1:
                    value = value.sum(f.dim, True)
            while value.shape and value.shape[0] == 1:
                value.squeeze_(0)
            total = value if total is None else total + value
        return total

    def __repr__(self):
        return '%s(%s)' % (type(self).__name__, ",\n\t".join([
            '({}, ...)'.format(frames) for frames in self]))


class Dice(object):
    """
    An implementation of the DiCE operator compatible with Pyro features.

    This implementation correctly handles:
    - scaled log-probability due to subsampling
    - independence in different ordinals due to iarange
    - weights due to parallel and sequential enumeration

    This assumes restricted dependency structure on the model and guide:
    variables outside of an :class:`~pyro.iarange` can never depend on
    variables inside that :class:`~pyro.iarange`.

    References:
    [1] Jakob Foerster, Greg Farquhar, Maruan Al-Shedivat, Tim Rocktaeschel,
        Eric P. Xing, Shimon Whiteson (2018)
        "DiCE: The Infinitely Differentiable Monte-Carlo Estimator"
        https://arxiv.org/abs/1802.05098

    :param pyro.poutine.trace.Trace guide_trace: A guide trace.
    :param ordering: A dictionary mapping model site names to ordinal values.
        Ordinal values may be any type that is (1) ``<=`` comparable and (2)
        hashable; the canonical ordinal is a ``frozenset`` of site names.
    """
    def __init__(self, guide_trace, ordering):
        log_denom = defaultdict(float)  # avoids double-counting when sequentially enumerating
        log_probs = defaultdict(list)  # accounts for upstream probabilties

        for name, site in guide_trace.nodes.items():
            if site["type"] != "sample":
                continue
            log_prob = site['score_parts'].score_function  # not scaled by subsampling
            if is_identically_zero(log_prob):
                continue

            ordinal = ordering[name]
            if site["infer"].get("enumerate"):
                if site["infer"]["enumerate"] == "sequential":
                    log_denom[ordinal] += math.log(site["infer"]["_enum_total"])
            else:  # site was monte carlo sampled
                log_prob = log_prob - log_prob.detach()
            log_probs[ordinal].append(log_prob)

        self.log_denom = log_denom
        self.log_probs = log_probs
        self._log_factors_cache = {}
        self._prob_cache = {}

    def _get_log_factors(self, target_ordinal):
        """
        Returns a list of DiCE factors at a given ordinal.
        """
        # memoize
        try:
            return self._log_factors_cache[target_ordinal]
        except KeyError:
            pass

        log_denom = 0
        for ordinal, term in self.log_denom.items():
            if not ordinal <= target_ordinal:  # not downstream
                log_denom += term  # term = log(# times this ordinal is counted)

        log_factors = [] if is_identically_zero(log_denom) else [-log_denom]
        for ordinal, term in self.log_probs.items():
            if ordinal <= target_ordinal:  # upstream
                log_factors += term  # term = [log(dice weight of this ordinal)]

        self._log_factors_cache[target_ordinal] = log_factors
        return log_factors

    def in_context(self, shape, ordinal):
        """
        Returns the DiCE operator at a given ordinal, summed to given shape.

        :param torch.Size shape: a target shape
        :param ordinal: an ordinal key that has been passed in to the
            ``ordering`` argument of the :class:`Dice` constructor.
        :return: the dice probability summed down to at most ``shape``.
            This should be broadcastable up to ``shape``.
        :rtype: torch.Tensor or float
        """
        # ignore leading 1's since they can be broadcast
        while shape and shape[0] == 1:
            shape = shape[1:]

        # memoize
        try:
            return self._prob_cache[shape, ordinal]
        except KeyError:
            pass

        log_factors = self._get_log_factors(ordinal)
        factors = [torch_exp(f) for f in log_factors]
        dice_prob = sumproduct(factors, shape)

        self._prob_cache[shape, ordinal] = dice_prob
        return dice_prob

    def compute_expectation(self, costs, use_einsum=True):
        """
        Returns a differentiable expected cost, summing over costs at given ordinals.

        :param dict costs: A dict mapping ordinals to lists of cost tensors
        :returns: a scalar expected cost
        :rtype: torch.Tensor or float
        """
        if use_einsum:
            exp_table = {}
            factors_table = defaultdict(list)
            for ordinal in costs:
                for log_factor in self._get_log_factors(ordinal):
                    if id(log_factor) not in exp_table:
                        factor = torch_exp(log_factor)
                        exp_table[id(log_factor)] = factor
                        factors_table[ordinal].append(factor)

        expected_cost = 0.
        if use_einsum:
            with shared_intermediates():
                for ordinal, cost_terms in costs.items():
                    factors = factors_table[ordinal]
                    for cost in cost_terms:
                        prob = sumproduct(factors, cost.shape, optimize=True,
                                          backend='pyro.ops._einsum')
                        mask = prob > 0
                        if torch.is_tensor(mask) and not mask.all():
                            cost, prob, mask = broadcast_all(cost, prob, mask)
                            prob = prob[mask]
                            cost = cost[mask]
                        expected_cost = expected_cost + (prob * cost).sum()
        else:
            for ordinal, cost_terms in costs.items():
                cost = sum(cost_terms)
                prob = self.in_context(cost.shape, ordinal)
                mask = prob > 0
                if torch.is_tensor(mask) and not mask.all():
                    cost, prob, mask = broadcast_all(cost, prob, mask)
                    prob = prob[mask]
                    cost = cost[mask]
                expected_cost = expected_cost + (prob * cost).sum()
        return expected_cost
