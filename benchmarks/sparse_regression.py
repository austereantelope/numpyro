import argparse
import os
import subprocess
import sys
import time

import jax
import jax.numpy as np
import numpy as onp
import pystan
from jax import random, device_get

import numpyro
import numpyro.distributions as dist
from numpyro.diagnostics import effective_sample_size
from numpyro.infer import NUTS, MCMC


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)


def set_logging(filename):
    tee = subprocess.Popen(['tee', '/tmp/' + filename + '.txt'], stdin=subprocess.PIPE)
    os.dup2(tee.stdin.fileno(), sys.stdout.fileno())
    os.dup2(tee.stdin.fileno(), sys.stderr.fileno())


def dot(X, Z):
    return np.dot(X, Z[..., None])[..., 0]


# Computes the NxN kernel matrix that corresponds to our quadratic regressor.
def kernel_matrix(X, X2, eta1, eta2, c):
    eta1sq = np.square(eta1)
    eta2sq = np.square(eta2)
    K1 = dot(X, X)
    K2 = dot(X2, X2)
    k1 = 0.5 * eta2sq * np.square(1.0 + K1)
    k2 = -0.5 * eta2sq * K2
    k3 = (eta1sq - eta2sq) * K1
    k4 = np.square(c) - 0.5 * eta2sq
    return k1 + k2 + k3 + k4


def model(X, Y, hypers):
    M, N = X.shape[1], X.shape[0]
    m0 = hypers['expected_sparsity']
    alpha_1, beta_1 = hypers['alpha_1'], hypers['beta_1']
    alpha_2, beta_2 = hypers['alpha_2'], hypers['beta_2']
    sigma = numpyro.sample("sigma", dist.HalfNormal(hypers['sigma_scale']))
    phi = (m0 / (M - m0)) * (sigma / np.sqrt(N))
    eta1_base = numpyro.sample("eta1_base", dist.HalfCauchy(1.))
    eta1 = phi * eta1_base
    msq = numpyro.sample("m_sq", dist.InverseGamma(alpha_1, beta_1))
    psi_sq = numpyro.sample("psi_sq", dist.InverseGamma(alpha_2, beta_2))

    eta2 = np.square(eta1) * np.sqrt(psi_sq) / msq

    lam = numpyro.sample("lambda", dist.HalfCauchy(np.ones(M)))
    kappa = np.sqrt(msq) * lam / np.sqrt(msq + np.square(eta1 * lam))

    # sample observation noise
    var_obs = numpyro.sample("var_obs", dist.InverseGamma(hypers['alpha_obs'], hypers['beta_obs']))

    # compute kernel
    kX = kappa * X
    kX2 = kappa * np.square(X)
    k = kernel_matrix(kX, kX2, eta1, eta2, hypers['c']) + var_obs * np.eye(N)
    assert k.shape == (N, N)

    # sample Y according to the standard gaussian process formula
    numpyro.sample("Y", dist.MultivariateNormal(loc=np.zeros(X.shape[0]), covariance_matrix=k),
                   obs=Y)


def stan_model(hypers):
    model_code = """
        data {{
          int<lower=1> N; // Number of data
          int<lower=1> P; // Number of covariates
          matrix[N, P] X;
          vector[N] Y;
          // vector[P] X[N];
        }}
        transformed data {{
         // Interaction global scale params
          real m0 = {expected_sparsity}; // Expected number of large slopes
          real<lower=0> c = {c}; // Intercept prior scale
          real sigma_scale = {sigma_scale};
          real alpha_1 = {alpha_1};
          real beta_1 = {beta_1};
          real alpha_2 = {alpha_2};
          real beta_2 = {beta_2};
          real alpha_obs = {alpha_obs};
          real beta_obs = {beta_obs};
          vector[N] mu = rep_vector(0, N);
          // vector[P] X2[N] = square(X);
          matrix[N, P] X2 = square(X);
        }}
        parameters {{
          vector<lower=0>[P] lambda;
          real<lower=0> m_sq; // Truncation level for local scale horseshoe
          real<lower=0> eta_1_base;
          real<lower=0> sigma; // Noise scale of response
          real<lower=0> psi_sq; // Interaction scale (selected ones)
          real<lower=0> var_obs;
        }}
        transformed parameters {{
          real<lower=0> eta_1;
          real<lower=0> eta_2;
          real psi = sqrt(psi_sq);
          vector[P] kappa;
          {{
            real phi = (m0 / (P - m0)) * (sigma / sqrt(1.0 * N));
            eta_1 = phi * eta_1_base; // eta_1 ~ cauchy(0, phi), global scale for linear effects
            kappa = m_sq * square(lambda) ./ (m_sq + square(eta_1) * square(lambda));
          }}
          eta_2 = square(eta_1) / m_sq * psi; // Global prior variance of interaction terms
        }}
        model {{
          matrix[N, N] L_K;
          matrix[N, N] K1 = diag_post_multiply(X, kappa) *  X';
          matrix[N, N] K2 = diag_post_multiply(X2, kappa) *  X2';
          matrix[N, N] K = .5 * square(eta_2) * square(K1 + 1.0) - .5 * square(eta_2) * K2 + (square(eta_1) -
              square(eta_2)) * K1 + square(c) - .5 * square(eta_2);

          var_obs ~ inv_gamma(alpha_obs, beta_obs);
          // diagonal elements
          for (n in 1:N)
            K[n, n] += var_obs;
          L_K = cholesky_decompose(K);
          lambda ~ cauchy(0, 1);
          eta_1_base ~ cauchy(0, 1);
          m_sq ~ inv_gamma(alpha_1, beta_1);
          sigma ~ normal(0, sigma_scale);
          psi_sq ~ inv_gamma(alpha_2, beta_2);
          Y ~ multi_normal_cholesky(mu, L_K);
        }}
    """.format(expected_sparsity=hypers['expected_sparsity'],
               c=hypers['c'],
               alpha_1=hypers['alpha_1'],
               beta_1=hypers['beta_1'],
               alpha_2=hypers['alpha_2'],
               beta_2=hypers['beta_2'],
               sigma_scale=hypers['sigma_scale'],
               alpha_obs=hypers['alpha_obs'],
               beta_obs=hypers['beta_obs'])
    print(model_code)

    model = pystan.StanModel(model_code=model_code)
    return model


def get_data(N=20, S=2, P=10, sigma_obs=0.05):
    assert S < P and P > 1 and S > 0
    onp.random.seed(0)

    X = onp.random.randn(N, P)
    # generate S coefficients with non-negligible magnitude
    W = 0.5 + 2.5 * onp.random.rand(S)
    # generate data using the S coefficients and a single pairwise interaction
    Y = onp.sum(X[:, 0:S] * W, axis=-1) + X[:, 0] * X[:, 1] + sigma_obs * onp.random.randn(N)
    Y -= onp.mean(Y)
    Y_std = onp.std(Y)

    assert X.shape == (N, P)
    assert Y.shape == (N,)

    return {
        'N': N,
        'P': P,
        'X': X,
        'Y': Y / Y_std,
        'expected_thetas': W / Y_std,
        'expected_pairwise': 1.0 / Y_std,
    }


def numpyro_inference(hypers, data, args):
    rng_key = random.PRNGKey(args.seed)
    bound_model = jax.partial(model, hypers=hypers)
    kernel = NUTS(bound_model)
    mcmc = MCMC(kernel, args.num_warmup, args.num_samples,
                num_chains=args.num_chains, progress_bar=not args.disable_progbar)
    tic = time.time()
    mcmc._compile(rng_key, data['X'], data['Y'], extra_fields=('num_steps',))
    print('MCMC (numpyro) compiling time:', time.time() - tic, '\n')
    tic = time.time()
    mcmc.warmup(rng_key, data['X'], data['Y'], extra_fields=('num_steps',))
    rng_key = mcmc._warmup_state.rng_key.copy()
    tic_run = time.time()
    mcmc.run(rng_key, data['X'], data['Y'], extra_fields=('num_steps',))
    mcmc._last_state.rng_key.copy()
    toc = time.time()
    mcmc.print_summary()
    print('\nMCMC (numpyro) elapsed time:', toc - tic)
    sampling_time = toc - tic_run
    num_leapfrogs = np.sum(mcmc.get_extra_fields()['num_steps'])
    print('num leapfrogs', num_leapfrogs)
    time_per_leapfrog = sampling_time / num_leapfrogs
    print('time per leapfrog', time_per_leapfrog)
    n_effs = [effective_sample_size(device_get(v)) for k, v in mcmc.get_samples(group_by_chain=True).items()]
    n_effs = onp.concatenate([onp.array([x]) if np.ndim(x) == 0 else x for x in n_effs])
    n_eff_mean = sum(n_effs) / len(n_effs)
    print('mean n_eff', n_eff_mean)
    time_per_eff_sample = sampling_time / n_eff_mean
    print('time per effective sample', time_per_eff_sample)
    return num_leapfrogs, n_eff_mean, toc - tic, time_per_leapfrog, time_per_eff_sample


def _get_pystan_sampling_time(filename):
    secs = None
    with open('/tmp/' + filename + '.txt', 'r') as f:
        for line in f:
            line = line.strip()
            if 'seconds (Sampling)' in line:
                secs = float(line.split()[0])
                break
    return secs


def stan_inference(hypers, data, args):
    log_filename = 'P={}.txt'.format(args.num_dimensions)
    set_logging(log_filename)
    tic = time.time()
    sm = stan_model(hypers)
    print('MCMC (stan) compiling time:', time.time() - tic, '\n')
    tic = time.time()
    fit = sm.sampling(data=data, iter=args.num_samples + args.num_warmup, warmup=args.num_warmup,
                      chains=args.num_chains, seed=args.seed)
    toc = time.time()
    print(fit)
    print('\nMCMC (stan) elapsed time:', toc - tic)
    sampling_time = _get_pystan_sampling_time(log_filename)
    sampler_params = fit.get_sampler_params(inc_warmup=False)
    num_leapfrogs = sum([p["n_leapfrog__"].sum() for p in sampler_params])
    print('num leapfrogs', num_leapfrogs)
    time_per_leapfrog = sampling_time / num_leapfrogs
    print('time per leapfrog', time_per_leapfrog)
    summary = fit.summary(pars=('lambda', 'm_sq', 'eta_1_base', 'sigma', 'psi_sq', 'var_obs'))['summary']
    n_effs = [row[8] for row in summary]
    n_eff_mean = sum(n_effs) / len(n_effs)
    print('mean n_eff', n_eff_mean)
    time_per_eff_sample = sampling_time / n_eff_mean
    print('time per effective sample', time_per_eff_sample)
    return num_leapfrogs, n_eff_mean, toc - tic, time_per_leapfrog, time_per_eff_sample


def main(args):
    data = get_data(N=args.num_data, P=args.num_dimensions, S=args.active_dimensions)
    hypers = {
        'expected_sparsity': max(1.0, args.num_dimensions / 10),
        'alpha_1': 3.0,
        'beta_1': 12.0,
        'alpha_2': 3.0,
        'beta_2': 3.0,
        'c': 1.,
        'sigma_scale': 1.,
        'alpha_obs': 3.,
        'beta_obs': 1.,
    }
    if args.backend == 'numpyro':
        result = numpyro_inference(hypers, data, args)
    else:
        result = stan_inference(hypers, data, args)

    out_filename = 'sparsereg_{}_{}_N={}_P={}_seed={}.txt'.format(args.backend,
                                                                  args.device,
                                                                  args.num_data,
                                                                  args.num_dimensions,
                                                                  args.seed)
    with open(os.path.join(DATA_DIR, out_filename), 'w') as f:
        f.write('\t'.join(['num_leapfrog', 'n_eff', 'total_time', 'time_per_leapfrog', 'time_per_eff_sample']))
        f.write('\n')
        f.write('\t'.join([str(x) for x in result]))
        f.write('\n')


if __name__ == "__main__":
    assert numpyro.__version__.startswith('0.2.1')
    parser = argparse.ArgumentParser(description="Sparse regression example")
    parser.add_argument("-n", "--num-samples", nargs="?", default=500, type=int)
    parser.add_argument("--num-warmup", nargs='?', default=500, type=int)
    parser.add_argument("--num-chains", nargs='?', default=1, type=int)
    parser.add_argument("--num-data", nargs='?', default=100, type=int)
    parser.add_argument("--num-dimensions", nargs='?', default=50, type=int)
    parser.add_argument("--active-dimensions", nargs='?', default=3, type=int)
    parser.add_argument("--seed", nargs='?', default=2019, type=int)
    parser.add_argument("--device", default='cpu', type=str, help='use "cpu" or "gpu".')
    parser.add_argument("--backend", default='numpyro', type=str, help='either "numpyro" or "stan"')
    parser.add_argument("--x64", action="store_true")
    parser.add_argument("--disable-progbar", action="store_true")
    args = parser.parse_args()

    numpyro.enable_x64(args.x64)
    numpyro.set_platform(args.device)
    numpyro.set_host_device_count(args.num_chains)

    main(args)
