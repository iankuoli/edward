from __future__ import print_function
import numpy as np
import tensorflow as tf

from blackbox.data import Data
from blackbox.util import kl_multivariate_normal, log_sum_exp

try:
    import prettytensor as pt
except ImportError:
    pass

class Inference:
    """
    Base class for inference methods.

    Arguments
    ----------
    model: Model
        probability model p(x, z)
    variational: Variational
        variational model q(z; lambda)
    data: Data, optional
        data x
    """
    def __init__(self, model, variational, data=Data()):
        self.model = model
        self.variational = variational
        self.data = data

    def run(self, n_iter=1000, n_minibatch=1, n_data=None,
            n_print=100, score=None):
        """
        Run inference algorithm.

        Arguments
        ----------
        n_iter: int, optional
            Number of iterations for optimization.
        n_minibatch: int, optional
            Number of samples from variational model for calculating
            stochastic gradients.
        n_data: int, optional
            Number of samples for data subsampling. Default is to use all
            the data.
        n_print: int, optional
            Number of iterations for each print progress.
        score: bool, optional
            Whether to force inference to use the score function
            gradient estimator. Otherwise default is to use the
            reparameterization gradient if available.
        """
        self.n_iter = n_iter
        self.n_minibatch = n_minibatch
        self.n_data = n_data
        self.n_print = n_print
        if score is None and hasattr(self.variational, 'reparam'):
            self.score = False
        else:
            self.score = True

        self.samples = tf.placeholder(shape=(self.n_minibatch, self.variational.num_vars),
                                      dtype=tf.float32,
                                      name='samples')
        self.elbos = tf.zeros([self.n_minibatch])

        if self.score:
            loss = self.build_score_loss()
        else:
            loss = self.build_reparam_loss()

        # Decay the scalar learning rate by 0.9 every 100 iterations
        global_step = tf.Variable(0, trainable=False)
        starter_learning_rate = 0.1
        learning_rate = tf.train.exponential_decay(starter_learning_rate,
                                            global_step,
                                            100, 0.9, staircase=True)

        update = tf.train.AdamOptimizer(learning_rate).minimize(
            loss, global_step=global_step)
        init = tf.initialize_all_variables()

        sess = tf.Session()
        sess.run(init)
        for t in range(self.n_iter):
            elbo = self.update(sess, update)
            self.print_progress(t, elbo, sess)

    def update(self, sess, update):
        if self.score:
            samples = self.variational.sample(self.samples.get_shape(), sess)
        else:
            samples = self.variational.sample_noise(self.samples.get_shape())

        _, elbo = sess.run([update, self.elbos], {self.samples: samples})
        return elbo

    def print_progress(self, t, elbos, sess):
        if t % self.n_print == 0:
            print("iter %d elbo %.2f " % (t, np.mean(elbos)))
            self.variational.print_params(sess)

    def build_score_loss(self):
        raise NotImplementedError()

    def build_reparam_loss(self):
        raise NotImplementedError()

class MFVI(Inference):
# TODO this isn't MFVI so much as VI where q is analytic
    """
    Mean-field variational inference
    (Ranganath et al., 2014; Kingma and Welling, 2014)
    """
    def __init__(self, *args, **kwargs):
        Inference.__init__(self, *args, **kwargs)

    def build_score_loss(self):
        """
        Loss function to minimize, whose gradient is a stochastic
        gradient based on the score function estimator.
        """
        if hasattr(self.variational, 'entropy'):
            # ELBO = E_{q(z; lambda)} [ log p(x, z) ] + H(q(z; lambda))
            # where entropy is analytic
            q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
            for i in range(self.variational.num_vars):
                q_log_prob += self.variational.log_prob_zi(i, self.samples)

            p_log_prob = self.model.log_prob(x, self.samples)
            q_entropy = self.variational.entropy()
            self.elbos = p_log_prob + q_entropy
            return tf.reduce_mean(q_log_prob * tf.stop_gradient(p_log_prob)) + \
                   q_entropy
        else:
            # ELBO = E_{q(z; lambda)} [ log p(x, z) - log q(z; lambda) ]
            q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
            for i in range(self.variational.num_vars):
                q_log_prob += self.variational.log_prob_zi(i, self.samples)

            x = self.data.sample(self.n_data)
            self.elbos = self.model.log_prob(x, self.samples) - q_log_prob
            return -tf.reduce_mean(q_log_prob * tf.stop_gradient(self.elbos))

    def build_reparam_loss(self):
        """
        Loss function to minimize, whose gradient is a stochastic
        gradient based on the reparameterization trick.
        """
        z = self.variational.reparam(self.samples)

        if hasattr(self.variational, 'entropy'):
            # ELBO = E_{q(z; lambda)} [ log p(x, z) ] + H(q(z; lambda))
            # where entropy is analytic
            x = self.data.sample(self.n_data)
            self.elbos = self.model.log_prob(x, z) + self.variational.entropy()
        else:
            # ELBO = E_{q(z; lambda)} [ log p(x, z) - log q(z; lambda) ]
            q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
            for i in range(self.variational.num_vars):
                q_log_prob += self.variational.log_prob_zi(i, z)

            x = self.data.sample(self.n_data)
            self.elbos = self.model.log_prob(x, z) - q_log_prob

        return -tf.reduce_mean(self.elbos)

class VAE(Inference):
    # TODO refactor it to better integrate into Inference
    def __init__(self, *args, **kwargs):
        Inference.__init__(self, *args, **kwargs)

    def initialize(self, n_data):
        """
        Parameters
        ----------
        n_data: int
            Number of samples for data subsampling.
        """
        self.n_data = n_data
        self.x = tf.placeholder(tf.float32, [self.n_data, 28 * 28])

        self.loss = self.build_loss()
        optimizer = tf.train.AdamOptimizer(1e-2, epsilon=1.0)
        # TODO move this to not rely on Pretty Tensor
        self.train = pt.apply_optimizer(optimizer, losses=[self.loss])

        init = tf.initialize_all_variables()
        sess = tf.Session()
        sess.run(init)
        return sess

    def update(self, sess):
        x = self.data.sample(self.n_data)
        _, loss_value = sess.run([self.train, self.loss], {self.x: x})
        return loss_value

    def build_loss(self):
        with tf.variable_scope("model") as scope:
            z = self.variational.sample([self.n_data, self.variational.num_vars],
                                        self.x)
            # ELBO = E_{q(z | x)} [ log p(x | z) ] - KL(q(z | x) || p(z))
            # In general, there should be a scale factor due to data
            # subsampling, so that
            # ELBO = N / M * ( ELBO using x_b )
            # where x^b is a mini-batch of x, with sizes M and N respectively.
            # This is absorbed into the learning rate.
            elbo = tf.reduce_sum(self.model.log_likelihood(self.x, z)) - \
                   kl_multivariate_normal(self.variational.mean,
                                          self.variational.stddev)

        return -elbo

class KLpq(Inference):
    """
    Kullback-Leibler(posterior, approximation) minimization
    using adaptive importance sampling.
    """
    def __init__(self, *args, **kwargs):
        Inference.__init__(self, *args, **kwargs)

    def build_score_loss(self):
        """
        Loss function to minimize, whose gradient is a stochastic
        gradient based on the score function estimator.
        """
        # loss = E_{q(z; lambda)} [ w_norm(z; lambda) *
        #                           ( log p(x, z) - log q(z; lambda) ) ]
        # where w_norm(z; lambda) = w(z; lambda) / sum_z( w(z; lambda) )
        # and w(z; lambda) = p(x, z) / q(z; lambda)
        #
        # gradient = - E_{q(z; lambda)} [ w_norm(z; lambda) *
        #                                 grad_{lambda} log q(z; lambda) ]
        q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
        for i in range(self.variational.num_vars):
            q_log_prob += self.variational.log_prob_zi(i, self.samples)

        # 1/B sum_{b=1}^B grad_log_q * w_norm
        # = 1/B sum_{b=1}^B grad_log_q * exp{ log(w_norm) }
        x = self.data.sample(self.n_data)
        log_w = self.model.log_prob(x, self.samples) - q_log_prob

        # normalized log importance weights
        log_w_norm = log_w - log_sum_exp(log_w)
        w_norm = tf.exp(log_w_norm)

        self.elbos = w_norm * log_w
        return -tf.reduce_mean(q_log_prob * tf.stop_gradient(w_norm))

    def build_reparam_loss(self):
        raise NotImplementedError("KLpq: this inference method does not "
          "implement a reparameterization gradient. "
          "Please call `.run()` with `score=True`.")
