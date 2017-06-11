""" This file defines policy optimization for a tensorflow policy. """
import copy
import logging
import os
import tempfile
from datetime import datetime
from collections import OrderedDict

import numpy as np
import random
import matplotlib.pyplot as plt
# NOTE: Order of these imports matters for some reason.
# Changing it can lead to segmentation faults on some machines.
import tensorflow as tf

try:
    import imageio
except ImportError:
    print 'imageio not found'
    imageio = None

from natsort import natsorted
from random import shuffle
from gps.algorithm.policy.tf_policy_lstm import TfPolicyLSTM
from gps.algorithm.policy_opt.config import POLICY_OPT_TF
from gps.algorithm.policy_opt.policy_opt import PolicyOpt
from gps.algorithm.policy_opt.policy_cloning_maml import PolicyCloningMAML
from gps.algorithm.policy_opt.tf_model_example import *
from gps.algorithm.policy_opt.tf_utils import TfSolver
from gps.sample.sample_list import SampleList
from gps.utility.demo_utils import xu_to_sample_list, extract_demo_dict, extract_demo_dict_multi
from gps.utility.general_utils import BatchSampler, compute_distance, mkdir_p, Timer

ANNEAL_INTERVAL = 20000 # this used to be 5000

class PolicyCloningLSTM(PolicyCloningMAML):
    """ Set up weighted neural network norm loss with learned parameters. """
    def __init__(self, hyperparams, dO, dU):
        config = copy.deepcopy(POLICY_OPT_TF)
        config.update(hyperparams)

        PolicyOpt.__init__(self, config, dO, dU)

        tf.set_random_seed(self._hyperparams['random_seed'])

        self.tf_iter = 0
        self.graph = tf.Graph()
        self.checkpoint_file = self._hyperparams['checkpoint_prefix']
        self.device_string = "/cpu:0"
        if self._hyperparams['use_gpu'] == 1:
            if not self._hyperparams.get('use_vision', False):
                gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.2)
                tf_config = tf.ConfigProto(gpu_options=gpu_options)
                self._sess = tf.Session(graph=self.graph, config=tf_config)
            else:
                self.gpu_device = self._hyperparams['gpu_id']
                self.device_string = "/gpu:" + str(self.gpu_device)
                # self._sess = tf.Session(graph=self.graph)
                gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.4)
                tf_config = tf.ConfigProto(gpu_options=gpu_options)
                self._sess = tf.Session(graph=self.graph, config=tf_config)
        else:
            self._sess = tf.Session(graph=self.graph)
        self.act_op = None  # mu_hat
        self.feat_op = None # features
        self.image_op = None # image
        self.loss_scalar = None
        self.obs_tensor = None
        self.precision_tensor = None
        self.action_tensor = None  # mu true
        self.solver = None
        self.feat_vals = None
        self.debug = None
        self.debug_vals = None
        self.bias = None
        self.scale = None
        self.reference_out = None
        self.norm_type = self._hyperparams.get('norm_type', False)
        self._hyperparams['network_params'].update({'norm_type': self.norm_type})
        self._hyperparams['network_params'].update({'decay': self._hyperparams.get('decay', 0.99)})
        # MAML hyperparams
        self.update_batch_size = self._hyperparams.get('update_batch_size', 1)
        self.eval_batch_size = self._hyperparams.get('eval_batch_size', 5)
        self.meta_batch_size = self._hyperparams.get('meta_batch_size', 10)
        self.num_updates = self._hyperparams.get('num_updates', 1)
        self.meta_lr = self._hyperparams.get('lr', 1e-3) #1e-3
        self.weight_decay = self._hyperparams.get('weight_decay', 0.005)
        self.demo_gif_dir = self._hyperparams.get('demo_gif_dir', None)
        
        self.T = self._hyperparams.get('T', 50)
        # List of indices for state (vector) data and image (tensor) data in observation.
        self.x_idx, self.img_idx, i = [], [], 0
        if 'obs_image_data' not in self._hyperparams['network_params']:
            self._hyperparams['network_params'].update({'obs_image_data': []})
        for sensor in self._hyperparams['network_params']['obs_include']:
            dim = self._hyperparams['network_params']['sensor_dims'][sensor]
            if sensor in self._hyperparams['network_params']['obs_image_data']:
                self.img_idx = self.img_idx + list(range(i, i+dim))
            else:
                self.x_idx = self.x_idx + list(range(i, i+dim))
            i += dim

        # For loading demos
        if hyperparams.get('agent', False):
            test_agent = hyperparams['agent']
            # test_agent = hyperparams['agent'][:1200]  # Required for sampling
            # test_agent.extend(hyperparams['agent'][-100:])
            test_agent = hyperparams['agent'][:300]  # Required for sampling
            test_agent.extend(hyperparams['agent'][-150:])
            if type(test_agent) is not list:
                test_agent = [test_agent]
        demo_file = hyperparams['demo_file']
        # demo_file = hyperparams['demo_file'][:100]
        # demo_file.extend(hyperparams['demo_file'][-100:])
        demo_file = hyperparams['demo_file'][:300]
        demo_file.extend(hyperparams['demo_file'][-150:])
        
        if hyperparams.get('agent', False):
            self.restore_iter = hyperparams.get('restore_iter', 0)
            self.extract_supervised_data(demo_file)

        if not hyperparams.get('test', False):
            self.init_network(self.graph, restore_iter=self.restore_iter)
            self.init_network(self.graph, restore_iter=self.restore_iter, prefix='Validation_')
        else:
            self.init_network(self.graph, prefix='Testing')

        with self.graph.as_default():
            self.saver = tf.train.Saver()
        
        with self.graph.as_default():
            init_op = tf.global_variables_initializer()
        self.run(init_op)
        with self.graph.as_default():
            tf.train.start_queue_runners(sess=self._sess)
        
        if self.restore_iter > 0:
            self.restore_model(hyperparams['save_dir'] + '_%d' % self.restore_iter)
            # import pdb; pdb.set_trace()
            if not hyperparams.get('test', False):
                self.update()
            # TODO: also implement resuming training from restored model
        else:
            self.update()
            # import pdb; pdb.set_trace()
        if not hyperparams.get('test', False):
            os._exit(1) # debugging

        # Initialize policy with noise
        self.var = self._hyperparams['init_var'] * np.ones(dU)
        # use test action for policy action
        self.policy = TfPolicyLSTM(dU, self.obsa, self.statea, self.actiona, self.obsb, self.stateb,
                               self.test_act_op, self.reference_tensor, self.reference_out,
                               self.feat_op, self.image_op, self.norm_type,
                               0.5*np.ones(dU), self._sess, self.graph, self.device_string,
                               use_vision=self._hyperparams.get('use_vision', True),
                            #   np.zeros(dU), self._sess, self.graph, self.device_string, 
                               copy_param_scope=self._hyperparams['copy_param_scope'])
        self.policy.scale = self.scale
        self.policy.bias = self.bias
        self.policy.x_idx = self.x_idx
        self.policy.img_idx = self.img_idx
        self.policy.T = self.T
        self.policy.update_batch_size = self.update_batch_size
        # Generate selected demos for preupdate pass during testing
        self.generate_testing_demos()
        self.eval_success_rate(test_agent)

        self.test_agent = None  # don't pickle agent
        self.val_demos = None # don't pickle demos
        self.train_demos = None
        self.demos = None
        if self._hyperparams.get('agent', False):
            del self._hyperparams['agent']

    def init_network(self, graph, input_tensors=None, restore_iter=0, prefix='Training_'):
        """ Helper method to initialize the tf networks used """
        with graph.as_default():
            image_tensors = None
            if self._hyperparams.get('use_vision', True):
                if 'Training' in prefix:
                    image_tensors = self.make_batch_tensor(self._hyperparams['network_params'], restore_iter=restore_iter)
                elif 'Validation' in prefix:
                    image_tensors = self.make_batch_tensor(self._hyperparams['network_params'], restore_iter=restore_iter, train=False)
            if image_tensors is not None:
                # image_tensors = tf.reshape(image_tensors, [self.meta_batch_size, (self.update_batch_size+self.eval_batch_size)*self.T, -1])
                # inputa = tf.slice(image_tensors, [0, 0, 0], [-1, self.update_batch_size*self.T, -1])
                # inputb = tf.slice(image_tensors, [0, self.update_batch_size*self.T, 0], [-1, -1, -1])
                inputa = image_tensors[:, :self.update_batch_size*self.T, :]
                inputb = image_tensors[:, self.update_batch_size*self.T:, :]
                input_tensors = {'inputa': inputa, 'inputb': inputb}
            else:
                input_tensors = None
            with Timer('building TF network'):
                result = self.construct_model(input_tensors=input_tensors, prefix=prefix, dim_input=self._dO, dim_output=self._dU,
                                          network_config=self._hyperparams['network_params'])
            # outputas, outputbs, test_outputa, lossesa, lossesb, flat_img_inputa, fp, moving_mean, moving_variance, moving_mean_test, moving_variance_test = result
            test_output, loss, flat_img_inputb = result
            if 'Testing' in prefix:
                self.obs_tensor = self.obsa
                self.state_tensor = self.statea
                self.action_tensor = self.actiona
                self.test_act_op = test_output # post-update output
                self.image_op = flat_img_inputb

            total_loss = tf.reduce_sum(loss) / tf.to_float(self.meta_batch_size)

            if 'Training' in prefix:
                self.total_loss = total_loss
            elif 'Validation' in prefix:
                self.val_total_loss = total_loss
            # self.val_total_loss1 = tf.contrib.copy_graph.get_copied_op(total_loss1, self.graph)
            # self.val_total_losses2 = [tf.contrib.copy_graph.get_copied_op(total_losses2[i], self.graph) for i in xrange(len(total_losses2))]
 
            # Initialize solver
            # mom1, mom2 = 0.9, 0.999 # adam defaults
            # self.global_step = tf.Variable(0, trainable=False)
            # learning_rate = tf.train.exponential_decay(self.meta_lr, self.global_step, ANNEAL_INTERVAL, 0.5, staircase=True)
            # update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            # with tf.control_dependencies(update_ops):
            # self.train_op = tf.train.AdamOptimizer(learning_rate).minimize(self.total_losses2[self.num_updates - 1], global_step=self.global_step)
            # flat_img_inputb = tf.reshape(flat_img_inputb, [self.meta_batch_size, self.T, 3, 80, 64])
            # flat_img_inputb = tf.transpose(flat_img_inputb, perm=[0,1,4,3,2])
            if 'Training' in prefix:
                self.train_op = tf.train.AdamOptimizer(self.meta_lr).minimize(self.total_loss)
                # Add summaries
                summ = [tf.summary.scalar(prefix + 'loss', self.total_loss)] # tf.scalar_summary('Learning rate', learning_rate)
                # train_summ.append(tf.scalar_summary('Moving Mean', self.moving_mean))
                # train_summ.append(tf.scalar_summary('Moving Variance', self.moving_variance))
                # train_summ.append(tf.scalar_summary('Moving Mean Test', self.moving_mean_test))
                # train_summ.append(tf.scalar_summary('Moving Variance Test', self.moving_variance_test))
                # for i in xrange(self.meta_batch_size):
                #     summ.append(tf.summary.image('Training_image_%d' % i, flat_img_inputb[i]*255.0, max_outputs=50))
                self.train_summ_op = tf.summary.merge(summ)
            elif 'Validation' in prefix:
                # Add summaries
                summ = [tf.summary.scalar(prefix + 'loss', self.val_total_loss)] # tf.scalar_summary('Learning rate', learning_rate)
                # train_summ.append(tf.scalar_summary('Moving Mean', self.moving_mean))
                # train_summ.append(tf.scalar_summary('Moving Variance', self.moving_variance))
                # train_summ.append(tf.scalar_summary('Moving Mean Test', self.moving_mean_test))
                # train_summ.append(tf.scalar_summary('Moving Variance Test', self.moving_variance_test))
                # for i in xrange(self.meta_batch_size):
                    # summ.append(tf.summary.image('Validation_image_%d' % i, flat_img_inputb[i]*255.0, max_outputs=50))
                self.val_summ_op = tf.summary.merge(summ)
    
    def construct_weights(self, dim_input=27, dim_output=7, network_config=None):
        n_layers = network_config.get('n_layers', 4) # TODO TODO this used to be 3.
        layer_size = network_config.get('layer_size', 100)  # TODO TODO This used to be 20.
        dim_hidden = (n_layers - 1)*[layer_size]
        dim_hidden.append(dim_output)
        lstm_size = self._hyperparams.get('lstm_size', 512)
        weights = {}
        self.conv_out_size = 0
        if self._hyperparams.get('use_vision', True):
            filter_size = 3 # used to be 2
            num_filters = network_config['num_filters']
            im_height = network_config['image_height']
            im_width = network_config['image_width']
            num_channels = network_config['image_channels']
            is_dilated = self._hyperparams.get('is_dilated', False)
            if is_dilated:
                self.conv_out_size = int(im_width*im_height*num_filters[2])
            else:
                self.conv_out_size = int(im_width/(8.0)*im_height/(8.0)*num_filters[2]) # 3 layers each with stride 2
            # self.conv_out_size = int(im_width/(16.0)*im_height/(16.0)*num_filters[3]) # 3 layers each with stride 2
    
            # conv weights
            # weights['wc1'] = get_he_weights([filter_size, filter_size, num_channels, num_filters[0]], name='wc1') # 5x5 conv, 1 input, 32 outputs
            # weights['wc2'] = get_he_weights([filter_size, filter_size, num_filters[0], num_filters[1]], name='wc2') # 5x5 conv, 32 inputs, 64 outputs
            # weights['wc3'] = get_he_weights([filter_size, filter_size, num_filters[1], num_filters[2]], name='wc3') # 5x5 conv, 32 inputs, 64 outputs
            weights['wc1'] = init_conv_weights_xavier([filter_size, filter_size, num_channels, num_filters[0]], name='wc1') # 5x5 conv, 1 input, 32 outputs
            weights['wc2'] = init_conv_weights_xavier([filter_size, filter_size, num_filters[0], num_filters[1]], name='wc2') # 5x5 conv, 32 inputs, 64 outputs
            weights['wc3'] = init_conv_weights_xavier([filter_size, filter_size, num_filters[1], num_filters[2]], name='wc3') # 5x5 conv, 32 inputs, 64 outputs
            # weights['wc4'] = init_conv_weights_xavier([filter_size, filter_size, num_filters[2], num_filters[3]], name='wc4') # 5x5 conv, 32 inputs, 64 outputs
    
            weights['bc1'] = init_bias([num_filters[0]], name='bc1')
            weights['bc2'] = init_bias([num_filters[1]], name='bc2')
            weights['bc3'] = init_bias([num_filters[2]], name='bc3')
            # weights['bc4'] = init_bias([num_filters[3]], name='bc4')
        
        # LSTM cell
        self.lstm = tf.nn.rnn_cell.BasicRNNCell(lstm_size)
        self.lstm_initial_state = safe_get('lstm_initial_state', initializer=tf.zeros([self.update_batch_size, self.lstm.state_size], dtype=tf.float32))
        
        # fc weights
        # in_shape = 40 # dimension after feature computation
        in_shape = self.lstm.output_size + self.conv_out_size + len(self.x_idx) # hard-coded for last conv layer output
        if self._hyperparams.get('color_hints', False):
            in_shape += 3
        for i in xrange(n_layers):
            weights['w_%d' % i] = init_weights([in_shape, dim_hidden[i]], name='w_%d' % i)
            # weights['w_%d' % i] = init_fc_weights_xavier([in_shape, dim_hidden[i]], name='w_%d' % i)
            weights['b_%d' % i] = init_bias([dim_hidden[i]], name='b_%d' % i)
            in_shape = dim_hidden[i]
        return weights

    def conv_forward(self, image_input, state_input, weights, update=False, is_training=True, network_config=None):
        norm_type = self.norm_type
        decay = network_config.get('decay', 0.9)
        use_dropout = self._hyperparams.get('use_dropout', False)
        prob = self._hyperparams.get('keep_prob', 0.5)
        is_dilated = self._hyperparams.get('is_dilated', False)
        # conv_layer_0, _, _ = norm(conv2d(img=image_input, w=weights['wc1'], b=weights['bc1'], strides=[1,2,2,1]), norm_type=norm_type, decay=decay, conv_id=0, is_training=is_training)
        # conv_layer_1, _, _ = norm(conv2d(img=conv_layer_0, w=weights['wc2'], b=weights['bc2']), norm_type=norm_type, decay=decay, conv_id=1, is_training=is_training)
        # conv_layer_2, moving_mean, moving_variance = norm(conv2d(img=conv_layer_1, w=weights['wc3'], b=weights['bc3']), norm_type=norm_type, decay=decay, conv_id=2, is_training=is_training)            
        if norm_type == 'vbn':
            if not use_dropout:
                conv_layer_0 = self.vbn(conv2d(img=image_input, w=weights['wc1'], b=weights['bc1'], strides=[1,2,2,1], is_dilated=is_dilated), name='vbn_1', update=update)
                conv_layer_1 = self.vbn(conv2d(img=conv_layer_0, w=weights['wc2'], b=weights['bc2'], strides=[1,2,2,1], is_dilated=is_dilated), name='vbn_2', update=update)
                conv_layer_2 = self.vbn(conv2d(img=conv_layer_1, w=weights['wc3'], b=weights['bc3'], strides=[1,2,2,1], is_dilated=is_dilated), name='vbn_3', update=update)       
            else:
                conv_layer_0 = dropout(self.vbn(conv2d(img=image_input, w=weights['wc1'], b=weights['bc1'], strides=[1,2,2,1], is_dilated=is_dilated), name='vbn_1', update=update), keep_prob=prob, is_training=is_training, name='dropout_1')
                conv_layer_1 = dropout(self.vbn(conv2d(img=conv_layer_0, w=weights['wc2'], b=weights['bc2'], strides=[1,2,2,1], is_dilated=is_dilated), name='vbn_2', update=update), keep_prob=prob, is_training=is_training, name='dropout_2')
                conv_layer_2 = dropout(self.vbn(conv2d(img=conv_layer_1, w=weights['wc3'], b=weights['bc3'], strides=[1,2,2,1], is_dilated=is_dilated), name='vbn_3', update=update), keep_prob=prob, is_training=is_training, name='dropout_3')       
        else:
            if True:#not use_dropout:
                conv_layer_0 = norm(conv2d(img=image_input, w=weights['wc1'], b=weights['bc1'], strides=[1,2,2,1], is_dilated=is_dilated), norm_type=norm_type, decay=decay, conv_id=0, is_training=is_training)
                conv_layer_1 = norm(conv2d(img=conv_layer_0, w=weights['wc2'], b=weights['bc2'], strides=[1,2,2,1], is_dilated=is_dilated), norm_type=norm_type, decay=decay, conv_id=1, is_training=is_training)
                conv_layer_2 = norm(conv2d(img=conv_layer_1, w=weights['wc3'], b=weights['bc3'], strides=[1,2,2,1], is_dilated=is_dilated), norm_type=norm_type, decay=decay, conv_id=2, is_training=is_training)       
                # conv_layer_3 = norm(conv2d(img=conv_layer_2, w=weights['wc4'], b=weights['bc4'], strides=[1,2,2,1]), norm_type=norm_type, decay=decay, conv_id=3, is_training=is_training)       
            else:
                conv_layer_0 = dropout(norm(conv2d(img=image_input, w=weights['wc1'], b=weights['bc1'], strides=[1,2,2,1], is_dilated=is_dilated), norm_type=norm_type, decay=decay, conv_id=0, is_training=is_training), keep_prob=prob, is_training=is_training, name='dropout_1')
                conv_layer_1 = dropout(norm(conv2d(img=conv_layer_0, w=weights['wc2'], b=weights['bc2'], strides=[1,2,2,1], is_dilated=is_dilated), norm_type=norm_type, decay=decay, conv_id=1, is_training=is_training), keep_prob=prob, is_training=is_training, name='dropout_2')
                conv_layer_2 = dropout(norm(conv2d(img=conv_layer_1, w=weights['wc3'], b=weights['bc3'], strides=[1,2,2,1], is_dilated=is_dilated), norm_type=norm_type, decay=decay, conv_id=2, is_training=is_training), keep_prob=prob, is_training=is_training, name='dropout_3')       

        conv_out_flat = tf.reshape(conv_layer_2, [-1, self.conv_out_size])
        # conv_out_flat = tf.reshape(conv_layer_3, [-1, self.conv_out_size])
        # if use_dropout:
            # conv_out_flat = dropout(conv_out_flat, keep_prob=0.8, is_training=is_training, name='dropout_input')
        conv_output = tf.concat(concat_dim=1, values=[conv_out_flat, state_input])
        return conv_output
        
    def lstm_forward(self, lstm_input, actions, is_training=False, network_config=None):
        lstm_input = tf.concat(1, [lstm_input, actions])
        lstm_input = tf.reshape(lstm_input, [-1, self.T, self.conv_out_size+len(self.x_idx) + self._dU])
        
        # LSTM forward
        state = self.lstm_initial_state
        lstm_outputs = []
        with tf.variable_scope('LSTM', reuse=None) as lstm_scope:
            for t in xrange(self.T):
                try:
                    lstm_output, state = self.lstm(lstm_input[:, t, :], state)
                except ValueError:
                    lstm_scope.reuse_variables()
                    lstm_output, state = self.lstm(lstm_input[:, t, :], state)
                lstm_output = tf.expand_dims(lstm_output, axis=1)
                lstm_outputs.append(lstm_output)
        lstm_output = tf.concat(1, lstm_outputs)
        return lstm_output
        
    def fc_forward(self, fc_input, weights, is_training=True, network_config=None):
        n_layers = network_config.get('n_layers', 4) # 3
        use_dropout = self._hyperparams.get('use_dropout', False)
        prob = self._hyperparams.get('keep_prob', 0.5)
        fc_output = tf.add(fc_input, 0)

        for i in xrange(n_layers):
            fc_output = tf.matmul(fc_output, weights['w_%d' % i]) + weights['b_%d' % i]
            if i != n_layers - 1:
                fc_output = tf.nn.relu(fc_output)
                if use_dropout:
                    fc_output = dropout(fc_output, keep_prob=prob, is_training=is_training)
        return fc_output

    def construct_model(self, input_tensors=None, prefix='Training_', dim_input=27, dim_output=7, batch_size=25, network_config=None):
        """
        An example a network in theano that has both state and image inputs, with the feature
        point architecture (spatial softmax + expectation).
        Args:
            dim_input: Dimensionality of input.
            dim_output: Dimensionality of the output.
            batch_size: Batch size.
            network_config: dictionary of network structure parameters
        Returns:
            A tfMap object that stores inputs, outputs, and scalar loss.
        """
        # List of indices for state (vector) data and image (tensor) data in observation.
        x_idx, img_idx, i = [], [], 0
        for sensor in network_config['obs_include']:
            dim = network_config['sensor_dims'][sensor]
            if sensor in network_config['obs_image_data']:
                img_idx = img_idx + list(range(i, i+dim))
            else:
                x_idx = x_idx + list(range(i, i+dim))
            i += dim
        
        if self._hyperparams.get('use_vision', True):
            if input_tensors is None:
                self.obsa = obsa = tf.placeholder(tf.float32, name='obsa') # meta_batch_size x update_batch_size x dim_input
                self.obsb = obsb = tf.placeholder(tf.float32, name='obsb')
            else:
                self.obsa = obsa = input_tensors['inputa'] # meta_batch_size x update_batch_size x dim_input
                self.obsb = obsb = input_tensors['inputb']
        else:
            self.obsa, self.obsb = None, None
        # Temporary in order to make testing work
        if not hasattr(self, 'statea'):
            self.statea = statea = tf.placeholder(tf.float32, name='statea')
            self.stateb = stateb = tf.placeholder(tf.float32, name='stateb')
            # self.inputa = inputa = tf.placeholder(tf.float32)
            # self.inputb = inputb = tf.placeholder(tf.float32)
            self.actiona = actiona = tf.placeholder(tf.float32, name='actiona')
            self.actionb = actionb = tf.placeholder(tf.float32, name='actionb')
            self.reference_tensor = reference_tensor = tf.placeholder(tf.float32, [self.T, self._dO], name='reference')
            if self.norm_type:
                self.phase = tf.placeholder(tf.bool, name='phase')
        else:
            statea = self.statea
            stateb = self.stateb
            # self.inputa = inputa = tf.placeholder(tf.float32)
            # self.inputb = inputb = tf.placeholder(tf.float32)
            actiona = self.actiona
            actionb = self.actionb
            reference_tensor = self.reference_tensor
        
        if self._hyperparams.get('use_vision', True):
            inputa = tf.concat(2, [statea, obsa])
            inputb = tf.concat(2, [stateb, obsb])
        else:
            inputa = statea
            inputb = stateb
        
        with tf.variable_scope('model', reuse=None) as training_scope:
            # Construct layers weight & bias
            if 'weights' not in dir(self):
                self.weights = weights = self.construct_weights(dim_input, dim_output, network_config=network_config)
                self.sorted_weight_keys = natsorted(self.weights.keys())
            else:
                training_scope.reuse_variables()
                weights = self.weights

            def batch_metalearn(inp, update=False):
                inputa, inputb, actiona, actionb = inp #image input
                inputa = tf.reshape(inputa, [-1, dim_input])
                inputb = tf.reshape(inputb, [-1, dim_input])
                actiona = tf.reshape(actiona, [-1, dim_output])
                actionb = tf.reshape(actionb, [-1, dim_output])
                
                # Convert to image dims
                if self._hyperparams.get('use_vision', True):
                    inputa, _, state_inputa = self.construct_image_input(inputa, x_idx, img_idx, network_config=network_config)
                    inputb, flat_img_inputb, state_inputb = self.construct_image_input(inputb, x_idx, img_idx, network_config=network_config)
                else:
                    flat_img_inputb = tf.add(inputb, 0)
                    
                if 'Training' in prefix:
                    # local_outputa, fp, moving_mean, moving_variance = self.forward(inputa, state_inputa, weights, network_config=network_config)
                    if self._hyperparams.get('use_vision', True):
                        inputa = self.conv_forward(inputa, state_inputa, weights, network_config=network_config)
                        inputb = self.conv_forward(inputb, state_inputb, weights, network_config=network_config)
                    local_lstm_outputa = self.lstm_forward(inputa, actiona, network_config=network_config)
                    inputb = tf.reshape(inputb, [-1, self.T, self.conv_out_size+len(self.x_idx)])
                    local_outputb = tf.reshape(tf.concat(2, [local_lstm_outputa, inputb]), [-1, self.conv_out_size+len(self.x_idx)+self.lstm.output_size])
                    local_output = self.fc_forward(local_outputb, weights, network_config=network_config)
                else:
                    # local_outputa, _, moving_mean_test, moving_variance_test = self.forward(inputa, state_inputa, weights, is_training=False, network_config=network_config)
                    if self._hyperparams.get('use_vision', True):
                        inputa = self.conv_forward(inputa, state_inputa, weights, update=update, is_training=False, network_config=network_config)
                        inputb = self.conv_forward(inputb, state_inputb, weights, update=update, is_training=False, network_config=network_config)
                    local_lstm_outputa = self.lstm_forward(inputa, actiona, is_training=False, network_config=network_config)
                    inputb = tf.reshape(inputb, [-1, self.T, self.conv_out_size+len(self.x_idx)])
                    local_outputb = tf.reshape(tf.concat(2, [local_lstm_outputa, inputb]), [-1, self.conv_out_size+len(self.x_idx)+self.lstm.output_size])
                    local_output = self.fc_forward(local_outputb, weights, is_training=False, network_config=network_config)
                local_loss = euclidean_loss_layer(local_output, actionb, None, behavior_clone=True)
                
                local_fn_output = [local_output, local_loss, flat_img_inputb]
                return local_fn_output
                
        if self.norm_type:
            # initialize batch norm vars.
            if self.norm_type == 'vbn':
                # Initialize VBN
                # Uncomment below to update the mean and mean_sq of the reference batch
                self.reference_out = batch_metalearn((reference_tensor, reference_tensor, actionb[0], actionb[0]), update=True)[2]
                # unused = batch_metalearn((reference_tensor, reference_tensor, actionb[0], actionb[0]), update=False)[3]
            else:
                unused = batch_metalearn((inputa[0], inputb[0], actiona[0], actionb[0]))
        
        # out_dtype = [tf.float32, [tf.float32]*num_updates, tf.float32, tf.float32, [tf.float32]*num_updates, tf.float32, tf.float32, tf.float32, tf.float32, tf.float32, tf.float32]
        out_dtype = [tf.float32, tf.float32, tf.float32]
        result = tf.map_fn(batch_metalearn, elems=(inputa, inputb, actiona, actionb), dtype=out_dtype)
        print 'Done with map.'
        return result
    
    def update(self):
        """
        Update (train) policy.
        """
        # TODO: Do we need to normalize the observations?
        PRINT_INTERVAL = 100
        TEST_PRINT_INTERVAL = PRINT_INTERVAL*5
        SUMMARY_INTERVAL = 100
        SAVE_INTERVAL = 1000
        TOTAL_ITERS = self._hyperparams['iterations']
        losses= []
        log_dir = self._hyperparams['log_dir']# + '_%s' % datetime.now().strftime('%Y-%m-%d_%H:%M:%S')
        # log_dir = self._hyperparams['log_dir'] # for debugging
        save_dir = self._hyperparams['save_dir'] #'_model' #'_model_ln'
        train_writer = tf.train.SummaryWriter(log_dir, self.graph)
        # actual training.
        with Timer('Training'):
            if self.restore_iter == 0:
                training_range = range(TOTAL_ITERS)
            else:
                training_range = range(self.restore_iter+1, TOTAL_ITERS)
            for itr in training_range:
                # TODO: need to make state and obs compatible
                state, tgt_mu = self.generate_data_batch(itr)
                statea = state[:, :self.update_batch_size*self.T, :]
                stateb = state[:, self.update_batch_size*self.T:, :]
                actiona = tgt_mu[:, :self.update_batch_size*self.T, :]
                actionb = tgt_mu[:, self.update_batch_size*self.T:, :]
                feed_dict = {self.statea: statea,
                            self.stateb: stateb,
                            self.actiona: actiona,
                            self.actionb: actionb}
                input_tensors = [self.train_op]
                # if self.use_batchnorm:
                #     feed_dict[self.phase] = 1
                if self.norm_type == 'vbn':
                    feed_dict[self.reference_tensor] = self.reference_batch
                    input_tensors.append(self.reference_out)
                if itr % SUMMARY_INTERVAL == 0 or itr % PRINT_INTERVAL == 0:
                    input_tensors.extend([self.train_summ_op, self.total_loss])
                result = self.run(input_tensors, feed_dict=feed_dict)
    
                if itr != 0 and itr % SUMMARY_INTERVAL == 0:
                    train_writer.add_summary(result[-2], itr)
                    losses.append(result[-1])
    
                if itr != 0 and itr % PRINT_INTERVAL == 0:
                    print 'Iteration %d: average loss is %.2f' % (itr, np.mean(losses))
                    losses = []

                if itr != 0 and itr % TEST_PRINT_INTERVAL == 0:
                    if len(self.val_idx) > 0:
                        input_tensors = [self.val_summ_op, self.val_total_loss]
                        val_state, val_act = self.generate_data_batch(itr, train=False)
                        statea = val_state[:, :self.update_batch_size*self.T, :]
                        stateb = val_state[:, self.update_batch_size*self.T:, :]
                        actiona = val_act[:, :self.update_batch_size*self.T, :]
                        actionb = val_act[:, self.update_batch_size*self.T:, :]
                        feed_dict = {self.statea: statea,
                                    self.stateb: stateb,
                                    self.actiona: actiona,
                                    self.actionb: actionb}
                        # if self.use_batchnorm:
                        #     feed_dict[self.phase] = 0
                        if self.norm_type == 'vbn':
                            feed_dict[self.reference_tensor] = self.reference_batch
                            input_tensors.append(self.reference_out)
                        results = self.run(input_tensors, feed_dict=feed_dict)
                        train_writer.add_summary(results[0], itr)
                        print 'Test results: average loss is %.2f' % (np.mean(results[1]))
                
                if itr != 0 and (itr % SAVE_INTERVAL == 0 or itr == training_range[-1]):
                    self.save_model(save_dir + '_%d' % itr)

        # Keep track of tensorflow iterations for loading solver states.
        self.tf_iter += self._hyperparams['iterations']
