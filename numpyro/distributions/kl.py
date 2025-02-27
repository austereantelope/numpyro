# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

# The implementation follows the design in PyTorch: torch.distributions.kl.py
#
# Copyright (c) 2016-     Facebook, Inc            (Adam Paszke)
# Copyright (c) 2014-     Facebook, Inc            (Soumith Chintala)
# Copyright (c) 2011-2014 Idiap Research Institute (Ronan Collobert)
# Copyright (c) 2012-2014 Deepmind Technologies    (Koray Kavukcuoglu)
# Copyright (c) 2011-2012 NEC Laboratories America (Koray Kavukcuoglu)
# Copyright (c) 2011-2013 NYU                      (Clement Farabet)
# Copyright (c) 2006-2010 NEC Laboratories America (Ronan Collobert, Leon Bottou, Iain Melvin, Jason Weston)
# Copyright (c) 2006      Idiap Research Institute (Samy Bengio)
# Copyright (c) 2001-2004 Idiap Research Institute (Ronan Collobert, Samy Bengio, Johnny Mariethoz)
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from multipledispatch import dispatch

from jax import lax
import jax.numpy as jnp
from jax.scipy.special import digamma, gammaln

from numpyro.distributions.continuous import Dirichlet, Normal
from numpyro.distributions.distribution import (
    Delta,
    Distribution,
    ExpandedDistribution,
    Independent,
    MaskedDistribution,
)
from numpyro.distributions.util import scale_and_mask, sum_rightmost


def kl_divergence(p, q):
    r"""
    Compute Kullback-Leibler divergence :math:`KL(p \| q)` between two distributions.
    """
    raise NotImplementedError


################################################################################
# KL Divergence Implementations
################################################################################


@dispatch(Distribution, ExpandedDistribution)
def kl_divergence(p, q):
    kl = kl_divergence(p, q.base_dist)
    shape = lax.broadcast_shapes(p.batch_shape, q.batch_shape)
    return jnp.broadcast_to(kl, shape)


@dispatch(ExpandedDistribution, Distribution)
def kl_divergence(p, q):
    kl = kl_divergence(p.base_dist, q)
    shape = lax.broadcast_shapes(p.batch_shape, q.batch_shape)
    return jnp.broadcast_to(kl, shape)


@dispatch(ExpandedDistribution, ExpandedDistribution)
def kl_divergence(p, q):
    kl = kl_divergence(p.base_dist, q.base_dist)
    shape = lax.broadcast_shapes(p.batch_shape, q.batch_shape)
    return jnp.broadcast_to(kl, shape)


@dispatch(Delta, Distribution)
def kl_divergence(p, q):
    return -q.log_prob(p.v)


@dispatch(Delta, ExpandedDistribution)
def kl_divergence(p, q):
    return -q.log_prob(p.v)


@dispatch(Independent, Independent)
def kl_divergence(p, q):
    shared_ndims = min(p.reinterpreted_batch_ndims, q.reinterpreted_batch_ndims)
    p_ndims = p.reinterpreted_batch_ndims - shared_ndims
    q_ndims = q.reinterpreted_batch_ndims - shared_ndims
    p = Independent(p.base_dist, p_ndims) if p_ndims else p.base_dist
    q = Independent(q.base_dist, q_ndims) if q_ndims else q.base_dist
    kl = kl_divergence(p, q)
    if shared_ndims:
        kl = sum_rightmost(kl, shared_ndims)
    return kl


@dispatch(MaskedDistribution, MaskedDistribution)
def kl_divergence(p, q):
    if p._mask is False or q._mask is False:
        mask = False
    elif p._mask is True:
        mask = q._mask
    elif q._mask is True:
        mask = p._mask
    elif p._mask is q._mask:
        mask = p._mask
    else:
        mask = p._mask & q._mask

    if mask is False:
        return 0.0
    if mask is True:
        return kl_divergence(p.base_dist, q.base_dist)
    kl = kl_divergence(p.base_dist, q.base_dist)
    return scale_and_mask(kl, mask=mask)


@dispatch(Normal, Normal)
def kl_divergence(p, q):
    var_ratio = jnp.square(p.scale / q.scale)
    t1 = jnp.square((p.loc - q.loc) / q.scale)
    return 0.5 * (var_ratio + t1 - 1 - jnp.log(var_ratio))


@dispatch(Dirichlet, Dirichlet)
def kl_divergence(p, q):
    # From http://bariskurt.com/kullback-leibler-divergence-between-two-dirichlet-and-beta-distributions/
    sum_p_concentration = p.concentration.sum(-1)
    sum_q_concentration = q.concentration.sum(-1)
    t1 = gammaln(sum_p_concentration) - gammaln(sum_q_concentration)
    t2 = (gammaln(p.concentration) - gammaln(q.concentration)).sum(-1)
    t3 = p.concentration - q.concentration
    t4 = digamma(p.concentration) - digamma(sum_p_concentration)[..., None]
    return t1 - t2 + (t3 * t4).sum(-1)
