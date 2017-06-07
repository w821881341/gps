import pickle
import os
import uuid

import numpy as np
import tensorflow as tf

from gps.algorithm.policy.tf_policy_maml import TfPolicyMAML


class TfPolicyLSTM(TfPolicyMAML):
    """
    A neural network policy implemented in tensor flow. The network output is
    taken to be the mean, and Gaussian noise is added on top of it.
    U = net.forward(obs) + noise, where noise ~ N(0, diag(var))
    Args:
        obs_tensor: tensor representing tf observation. Used in feed dict for forward pass.
        act_op: tf op to execute the forward pass. Use sess.run on this op.
        var: Du-dimensional noise variance vector.
        sess: tf session.
        device_string: tf device string for running on either gpu or cpu.
    """
    def __init__(self, *args, **kwargs):
        super(TfPolicyLSTM, self).__init__(*args, **kwargs)

    def act(self, x, obs, t, noise, idx):
        """
        Return an action for a state.
        Args:
            x: State vector.
            obs: Observation vector.
            t: Time step.
            noise: Action noise. This will be scaled by the variance.
            idx: The index of the task. Use this to get the demos.
        """

        # Normalize obs.
        if len(obs.shape) == 1:
            obs = np.expand_dims(obs, axis=0)
        if self.scale is not None and self.bias is not None:
            obs[:, self.x_idx] = obs[:, self.x_idx].dot(self.scale) + self.bias
        if self.use_vision:
            obs[:, self.img_idx] /= 255.0
            state = obs[:, self.x_idx]
            obs = obs[:, self.img_idx]
        # This following code seems to be buggy
        # TODO: figure out why this doesn't work
        # if t == 0:
        #     assert hasattr(self, 'fast_weights_value')
        #     self.set_copy_params(self.fast_weights_value[idx])
        # if self.batch_norm:
        #     action_mean = self.run(self.act_op, feed_dict={self.inputa: obs, self.phase_op: 0}) # testing
        # else:
        if self.norm_type == 'vbn':
            assert hasattr(self, 'reference_batch')
            if self.reference_out is not None:
                action_mean = self.run([self.reference_out, self.act_op], feed_dict={self.obsa: obs, self.statea: state, self.reference_op: self.reference_batch})[1]
            else:
                action_mean = self.run(self.act_op, feed_dict={self.obsa: obs, self.statea: state, self.reference_op: self.reference_batch})
        # action_mean = self.run(self.act_op, feed_dict={self.inputa: obs}) #need to set act_op to be act_op_b if using set_params
        else:
            if self.use_vision:
                assert hasattr(self, 'selected_demoO')
                assert hasattr(self, 'T')
                selected_obs = self.selected_demoO[idx].astype(np.float32)
                selected_obs /= 255.0
                tiled_obs = np.tile(np.expand_dims(obs, axis=0), (1, self.update_batch_size*self.T, 1))
                tiled_state = np.tile(np.expand_dims(state, axis=0), (1, self.update_batch_size*self.T, 1))
                action_mean = self.run(self.act_op, feed_dict={self.obsa: selected_obs,
                                                              self.statea: self.selected_demoX[idx],
                                                              self.actiona: self.selected_demoU[idx],
                                                              self.obsb: tiled_obs,
                                                              self.stateb: tiled_state})[:, 0, :]
            else:
                tiled_state = np.tile(np.expand_dims(state, axis=0), (1, self.update_batch_size*self.T, 1))
                action_mean = self.run(self.act_op, feed_dict={self.statea: self.selected_demoX[idx],
                                                              self.actiona: self.selected_demoU[idx],
                                                              self.stateb: tiled_state})[:, 0, :]
        if noise is None:
            u = action_mean
        else:
            u = action_mean + self.chol_pol_covar.T.dot(noise)
        return np.squeeze(action_mean), np.squeeze(u)  # the DAG computations are batched by default, but we use batch size 1.