""" This file provides an example tensorflow network used to define a policy. """
from __future__ import division
import tensorflow as tf
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_util
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import array_ops
# from tensorflow.contrib.layers.python.layers import utils
from gps.algorithm.policy_opt.tf_utils import TfMap
import numpy as np

def safe_get(name, *args, **kwargs):
    """ Same as tf.get_variable, except flips on reuse_variables automatically """
    try:
        return tf.get_variable(name, *args, **kwargs)
    except ValueError:
        tf.get_variable_scope().reuse_variables()
        return tf.get_variable(name, *args, **kwargs)

def init_weights(shape, name=None):
    shape = tuple(shape)
    weights = np.random.normal(scale=0.01, size=shape).astype('f')
    return safe_get(name, list(shape), initializer=tf.constant_initializer(weights), dtype=tf.float32)
    
def init_bias(shape, name=None):
    return safe_get(name, initializer=tf.zeros(shape, dtype=tf.float32))

def init_fc_weights_xavier(shape, name=None):
    fc_initializer =  tf.contrib.layers.xavier_initializer(dtype=tf.float32)
    if len(shape) == 3:
        shape = [1] + shape
    return safe_get(name, list(shape), initializer=fc_initializer, dtype=tf.float32)

def init_conv_weights_xavier(shape, name=None):
    conv_initializer =  tf.contrib.layers.xavier_initializer_conv2d(dtype=tf.float32)
    return safe_get(name, list(shape), initializer=conv_initializer, dtype=tf.float32)
    
def init_fc_weights_snn(shape, name=None):
    weights = np.random.normal(scale=np.sqrt(1.0/shape[0]), size=shape).astype('f')
    return safe_get(name, list(shape), initializer=tf.constant_initializer(weights), dtype=tf.float32)

def init_conv_weights_snn(shape, name=None):
    weights = np.random.normal(scale=np.sqrt(1.0/(shape[0]*shape[1]*shape[2])), size=shape).astype('f')
    return safe_get(name, list(shape), initializer=tf.constant_initializer(weights), dtype=tf.float32)

def batched_matrix_vector_multiply(vector, matrix):
    """ computes x^T A in mini-batches. """
    vector_batch_as_matricies = tf.expand_dims(vector, [1])
    mult_result = tf.matmul(vector_batch_as_matricies, matrix)
    squeezed_result = tf.squeeze(mult_result, [1])
    return squeezed_result

def euclidean_loss_layer(a, b, precision, multiplier=100.0, behavior_clone=False, use_l1=False, eps=0.01):
    """ Math:  out = (action - mlp_out)'*precision*(action-mlp_out)
                    = (u-uhat)'*A*(u-uhat)"""
    # scale_factor = tf.constant(2*batch_size, dtype='float')
    multiplier = tf.constant(multiplier, dtype='float') #for bc #10000
    if not behavior_clone:
        uP = batched_matrix_vector_multiply(a-b, precision)
        return tf.reduce_mean(uP*(a-b))
    else:
        uP =a*multiplier-b*multiplier
        if use_l1:
            return tf.reduce_mean(eps*tf.square(uP) + tf.abs(uP))
        # return tf.reduce_mean(uP*uP)  # this last dot product is then summed, so we just the sum all at once.
        return tf.reduce_mean(tf.square(uP))

def acosine_loss(a, b, weights=1.0):
    a = tf.nn.l2_normalize(a, dim=1)
    b = tf.nn.l2_normalize(b, dim=1)
    return tf.reduce_mean(tf.acos(tf.losses.cosine_distance(a, b, dim=1, weights=weights)))

def get_input_layer(dim_input, dim_output, behavior_clone=False):
    """produce the placeholder inputs that are used to run ops forward and backwards.
        net_input: usually an observation.
        action: mu, the ground truth actions we're trying to learn.
        precision: precision matrix used to commpute loss."""
    net_input = tf.placeholder(tf.float32, [None, dim_input], name='nn_input')
    action = tf.placeholder(tf.float32, [None, dim_output], name='action')
    if not behavior_clone:
        precision = tf.placeholder(tf.float32, [None, dim_output, dim_output], name='precision')
    else:
        precision = None
    return net_input, action, precision


def get_mlp_layers(mlp_input, number_layers, dimension_hidden, batch_norm=False, decay=0.9, is_training=True):
    """compute MLP with specified number of layers.
        math: sigma(Wx + b)
        for each layer, where sigma is by default relu"""
    cur_top = mlp_input
    weights = []
    biases = []
    with tf.variable_scope(tf.get_variable_scope()) as vscope:
        for layer_step in range(0, number_layers):
            in_shape = cur_top.get_shape().dims[1].value
            cur_weight = init_weights([in_shape, dimension_hidden[layer_step]], name='w_' + str(layer_step))
            cur_bias = init_bias([dimension_hidden[layer_step]], name='b_' + str(layer_step))
            weights.append(cur_weight)
            biases.append(cur_bias)
            cur_top = tf.matmul(cur_top, cur_weight) + cur_bias
            if layer_step != number_layers-1:  # final layer has no RELU
                if not batch_norm:
                    cur_top = tf.nn.relu(cur_top)
                else:
                    if is_training:
                        with tf.variable_scope('bn_layer_%d' % layer_step) as vs:
                            try:
                                cur_top = tf.contrib.layers.batch_norm(cur_top, is_training=True, center=True,
                                    scale=False, decay=decay, activation_fn=tf.nn.relu, updates_collections=None, scope=vs)
                            except ValueError:
                                cur_top = tf.contrib.layers.batch_norm(cur_top, is_training=True, center=True,
                                    scale=False, decay=decay, activation_fn=tf.nn.relu, updates_collections=None, scope=vs, reuse=True)
                    else:
                        with tf.variable_scope('bn_layer_%d' % layer_step) as vs:
                            cur_top = tf.contrib.layers.batch_norm(cur_top, is_training=False, center=True,
                                    scale=False, decay=decay, activation_fn=tf.nn.relu, updates_collections=None, scope=vs, reuse=True)
    return cur_top, weights, biases


def get_loss_layer(mlp_out, action, precision, batch_size, behavior_clone=False):
    """The loss layer used for the MLP network is obtained through this class."""
    return euclidean_loss_layer(a=action, b=mlp_out, precision=precision, behavior_clone=behavior_clone)


def example_tf_network(dim_input=27, dim_output=7, batch_size=25, network_config=None):
    """
    An example of how one might want to specify a network in tensorflow.

    Args:
        dim_input: Dimensionality of input.
        dim_output: Dimensionality of the output.
        batch_size: Batch size.
    Returns:
        a TfMap object used to serialize, inputs, outputs, and loss.
    """
    n_layers = network_config.get('n_layers', 3)
    dim_hidden = network_config.get('dim_hidden', 40)
    behavior_clone = network_config.get('bc', False)
    batch_norm = network_config.get('batch_norm', False)
    decay = network_config.get('decay', 0.9)
    dim_hidden = (n_layers - 1) * [dim_hidden]
    dim_hidden.append(dim_output)
    nn_input, action, precision = get_input_layer(dim_input, dim_output, behavior_clone)
    mlp_applied, weights_FC, biases_FC = get_mlp_layers(nn_input, n_layers, dim_hidden, batch_norm=batch_norm, decay=decay, is_training=True)
    test_output, _, _ = get_mlp_layers(nn_input, n_layers, dim_hidden, batch_norm=batch_norm, decay=decay, is_training=False)
    # test_output = None
    fc_vars = weights_FC + biases_FC
    loss_out = get_loss_layer(mlp_out=mlp_applied, action=action, precision=precision, batch_size=batch_size, behavior_clone=behavior_clone)
    val_loss = get_loss_layer(mlp_out=test_output, action=action, precision=precision, batch_size=1, behavior_clone=behavior_clone)
    return TfMap.init_from_lists([nn_input, action, precision], [mlp_applied, test_output], [weights_FC], [loss_out, val_loss]), fc_vars, []


def multi_modal_network(dim_input=27, dim_output=7, batch_size=25, network_config=None):
    """
    An example a network in theano that has both state and image inputs.

    Args:
        dim_input: Dimensionality of input.
        dim_output: Dimensionality of the output.
        batch_size: Batch size.
        network_config: dictionary of network structure parameters
    Returns:
        A tfMap object that stores inputs, outputs, and scalar loss.
    """
    n_layers = network_config.get('n_layers', 3)
    layer_size = network_config.get('layer_size', 40)
    dim_hidden = (n_layers - 1)*[layer_size]
    dim_hidden.append(dim_output)
    # pool_size = network_config.get('pool_size', 2)
    filter_size = network_config.get('filter_size', 2)
    behavior_clone = network_config.get('bc', False)
    norm_type = network_config.get('norm_type', False)
    decay = network_config.get('decay', 0.9)

    # List of indices for state (vector) data and image (tensor) data in observation.
    x_idx, img_idx, i = [], [], 0
    for sensor in network_config['obs_include']:
        dim = network_config['sensor_dims'][sensor]
        if sensor in network_config['obs_image_data']:
            img_idx = img_idx + list(range(i, i+dim))
        else:
            x_idx = x_idx + list(range(i, i+dim))
        i += dim

    nn_input, action, precision = get_input_layer(dim_input, dim_output, behavior_clone)

    state_input = nn_input[:, 0:x_idx[-1]+1]
    flat_image_input = nn_input[:, x_idx[-1]+1:img_idx[-1]+1]

    # image goes through 2 convnet layers
    num_filters = network_config['num_filters']

    im_height = network_config['image_height']
    im_width = network_config['image_width']
    num_channels = network_config['image_channels']
    image_input = tf.reshape(flat_image_input, [-1, im_width, im_height, num_channels])

    # we pool twice, each time reducing the image size by a factor of 2.
    # conv_out_size = int(im_width/(2.0*pool_size)*im_height/(2.0*pool_size)*num_filters[1])
    conv_out_size = int(im_width/(8.0)*im_height/(8.0)*num_filters[1])
    first_dense_size = conv_out_size + len(x_idx)

    # Store layers weight & bias
    weights = {
        # 'wc1': get_xavier_weights([filter_size, filter_size, num_channels, num_filters[0]], (pool_size, pool_size), name='wc1'), # 5x5 conv, 1 input, 32 outputs
        # 'wc2': get_xavier_weights([filter_size, filter_size, num_filters[0], num_filters[1]], (pool_size, pool_size), name='wc2'), # 5x5 conv, 32 inputs, 64 outputs
        'wc1': init_conv_weights_xavier([filter_size, filter_size, num_channels, num_filters[0]], name='wc1'),
        'wc2': init_conv_weights_xavier([filter_size, filter_size, num_filters[0], num_filters[1]], name='wc2'),
        'wc3': init_conv_weights_xavier([filter_size, filter_size, num_filters[1], num_filters[2]], name='wc3'),
    }

    biases = {
        'bc1': init_bias([num_filters[0]], name='bc1'),
        'bc2': init_bias([num_filters[1]], name='bc2'),
        'bc3': init_bias([num_filters[2]], name='bc3'),
    }

    conv_layer_0 = norm(conv2d(img=image_input, w=weights['wc1'], b=biases['bc1'], strides=[1,2,2,1]), norm_type=norm_type, decay=decay, conv_id=0)

    # conv_layer_0 = max_pool(conv_layer_0, k=pool_size)

    conv_layer_1 = norm(conv2d(img=conv_layer_0, w=weights['wc2'], b=biases['bc2'], strides=[1,2,2,1]), norm_type=norm_type, decay=decay, conv_id=1)

    # conv_layer_1 = max_pool(conv_layer_1, k=pool_size)

    conv_layer_2 = norm(conv2d(img=conv_layer_1, w=weights['wc3'], b=biases['bc3'], strides=[1,2,2,1]), norm_type=norm_type, decay=decay, conv_id=2)

    conv_out_flat = tf.reshape(conv_layer_2, [-1, conv_out_size])

    fc_input = tf.concat(axis=1, values=[conv_out_flat, state_input])
    
    fc_output, weights_FC, biases_FC = get_mlp_layers(fc_input, n_layers, dim_hidden, batch_norm=False, decay=decay)

    loss = euclidean_loss_layer(a=action, b=fc_output, precision=precision, behavior_clone=behavior_clone)
    # training and testing the same (assuming using layernorm)
    nnet = TfMap.init_from_lists([nn_input, action, precision], [fc_output, fc_output], [weights], [loss, loss], image=flat_image_input)
    last_conv_vars = fc_input
    fc_vars = weights_FC + biases_FC
    return nnet, fc_vars, last_conv_vars

def multi_modal_network_fp(dim_input=27, dim_output=7, batch_size=25, network_config=None):
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
    n_layers = 3 # TODO TODO this used to be 3.
    layer_size = 20  # TODO TODO This used to be 20.
    dim_hidden = (n_layers - 1)*[layer_size]
    dim_hidden.append(dim_output)
    pool_size = 2
    filter_size = 5
    behavior_clone = network_config.get('bc', False)
    norm_type = network_config.get('norm_type', False)
    decay = network_config.get('decay', 0.9)

    # List of indices for state (vector) data and image (tensor) data in observation.
    x_idx, img_idx, i = [], [], 0
    for sensor in network_config['obs_include']:
        dim = network_config['sensor_dims'][sensor]
        if sensor in network_config['obs_image_data']:
            img_idx = img_idx + list(range(i, i+dim))
        else:
            x_idx = x_idx + list(range(i, i+dim))
        i += dim
    nn_input, action, precision = get_input_layer(dim_input, dim_output, behavior_clone)

    state_input = nn_input[:, 0:x_idx[-1]+1]
    flat_image_input = nn_input[:, x_idx[-1]+1:img_idx[-1]+1]

    # image goes through 3 convnet layers
    num_filters = network_config['num_filters']

    im_height = network_config['image_height']
    im_width = network_config['image_width']
    num_channels = network_config['image_channels']
    image_input = tf.reshape(flat_image_input, [-1, num_channels, im_width, im_height])
    image_input = tf.transpose(image_input, perm=[0,3,2,1])

    # we pool twice, each time reducing the image size by a factor of 2.
    conv_out_size = int(im_width/(2.0*pool_size)*im_height/(2.0*pool_size)*num_filters[1])
    first_dense_size = conv_out_size + len(x_idx)

    # Store layers weight & bias
    with tf.variable_scope('conv_params'):
        weights = {
            # 'wc1': init_weights([filter_size, filter_size, num_channels, num_filters[0]], name='wc1'), # 5x5 conv, 1 input, 32 outputs
            # 'wc2': init_weights([filter_size, filter_size, num_filters[0], num_filters[1]], name='wc2'), # 5x5 conv, 32 inputs, 64 outputs
            # 'wc3': init_weights([filter_size, filter_size, num_filters[1], num_filters[2]], name='wc3'), # 5x5 conv, 32 inputs, 64 outputs
            'wc1': get_he_weights([filter_size, filter_size, num_channels, num_filters[0]], name='wc1'), # 5x5 conv, 1 input, 32 outputs
            'wc2': get_he_weights([filter_size, filter_size, num_filters[0], num_filters[1]], name='wc2'), # 5x5 conv, 32 inputs, 64 outputs
            'wc3': get_he_weights([filter_size, filter_size, num_filters[1], num_filters[2]], name='wc3'), # 5x5 conv, 32 inputs, 64 outputs
        }

        biases = {
            'bc1': init_bias([num_filters[0]], name='bc1'),
            'bc2': init_bias([num_filters[1]], name='bc2'),
            'bc3': init_bias([num_filters[2]], name='bc3'),
        }

    fc_inputs = []
    is_training_list = [True, False]
    for is_training in is_training_list:
        conv_layer_0 = norm(conv2d(img=image_input, w=weights['wc1'], b=biases['bc1'], strides=[1,2,2,1]), norm_type=norm_type, decay=decay, conv_id=0, is_training=is_training)
        conv_layer_1 = norm(conv2d(img=conv_layer_0, w=weights['wc2'], b=biases['bc2']), norm_type=norm_type, decay=decay, conv_id=1, is_training=is_training)
        conv_layer_2 = norm(conv2d(img=conv_layer_1, w=weights['wc3'], b=biases['bc3']), norm_type=norm_type, decay=decay, conv_id=2, is_training=is_training)
        
        if is_training:
            training_conv_layer_2 = conv_layer_2

        _, num_rows, num_cols, num_fp = conv_layer_2.get_shape()
        num_rows, num_cols, num_fp = [int(x) for x in [num_rows, num_cols, num_fp]]
        x_map = np.empty([num_rows, num_cols], np.float32)
        y_map = np.empty([num_rows, num_cols], np.float32)

        for i in range(num_rows):
            for j in range(num_cols):
                x_map[i, j] = (i - num_rows / 2.0) / num_rows
                y_map[i, j] = (j - num_cols / 2.0) / num_cols

        x_map = tf.convert_to_tensor(x_map)
        y_map = tf.convert_to_tensor(y_map)

        x_map = tf.reshape(x_map, [num_rows * num_cols])
        y_map = tf.reshape(y_map, [num_rows * num_cols])

        # rearrange features to be [batch_size, num_fp, num_rows, num_cols]
        features = tf.reshape(tf.transpose(conv_layer_2, [0,3,1,2]),
                              [-1, num_rows*num_cols])
        softmax = tf.nn.softmax(features)

        fp_x = tf.reduce_sum(tf.multiply(x_map, softmax), [1], keep_dims=True)
        fp_y = tf.reduce_sum(tf.multiply(y_map, softmax), [1], keep_dims=True)

        fp = tf.reshape(tf.concat(axis=1, values=[fp_x, fp_y]), [-1, num_fp*2])

        fc_input = tf.concat(axis=1, values=[fp, state_input]) # TODO - switch these two?
        fc_inputs.append(fc_input)

    fc_output, weights_FC, biases_FC = get_mlp_layers(fc_inputs[0], n_layers, dim_hidden, batch_norm=False, decay=decay, is_training=True)
    test_output, _, _ = get_mlp_layers(fc_inputs[1], n_layers, dim_hidden, batch_norm=False, decay=decay, is_training=False)
    fc_vars = weights_FC + biases_FC
    for i in xrange(n_layers):
        weights['wfc%d' % i] = weights_FC[i]
        weights['bfc%d' % i] = biases_FC[i]
    weights.update(biases)

    loss = euclidean_loss_layer(a=action, b=fc_output, precision=precision, behavior_clone=behavior_clone)
    val_loss = euclidean_loss_layer(a=action, b=test_output, precision=precision, behavior_clone=behavior_clone)
    
    nnet = TfMap.init_from_lists([nn_input, action, precision], [fc_output, test_output], [weights], [loss, val_loss], fp=fp, image=flat_image_input, debug=training_conv_layer_2) #this is training conv layer
    last_conv_vars = fc_inputs[0] #training fc input

    return nnet, fc_vars, last_conv_vars

def multi_modal_network_fp_large(dim_input=27, dim_output=7, batch_size=25, network_config=None):
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
    n_layers = 5 # TODO TODO this used to be 3.
    layer_size = 50  # TODO TODO This used to be 20.
    dim_hidden = (n_layers - 1)*[layer_size]
    dim_hidden.append(dim_output)
    pool_size = 2
    filter_size = 5
    behavior_clone = network_config.get('bc', False)
    norm_type = network_config.get('norm_type', False)
    decay = network_config.get('decay', 0.9)

    # List of indices for state (vector) data and image (tensor) data in observation.
    x_idx, img_idx, i = [], [], 0
    for sensor in network_config['obs_include']:
        dim = network_config['sensor_dims'][sensor]
        if sensor in network_config['obs_image_data']:
            img_idx = img_idx + list(range(i, i+dim))
        else:
            x_idx = x_idx + list(range(i, i+dim))
        i += dim
    nn_input, action, precision = get_input_layer(dim_input, dim_output, behavior_clone)

    state_input = nn_input[:, 0:x_idx[-1]+1]
    flat_image_input = nn_input[:, x_idx[-1]+1:img_idx[-1]+1]

    # image goes through 3 convnet layers
    num_filters = network_config['num_filters']

    im_height = network_config['image_height']
    im_width = network_config['image_width']
    num_channels = network_config['image_channels']
    image_input = tf.reshape(flat_image_input, [-1, num_channels, im_width, im_height])
    image_input = tf.transpose(image_input, perm=[0,3,2,1])

    # we pool twice, each time reducing the image size by a factor of 2.
    conv_out_size = int(im_width/(2.0*pool_size)*im_height/(2.0*pool_size)*num_filters[1])
    first_dense_size = conv_out_size + len(x_idx)

    # Store layers weight & bias
    with tf.variable_scope('conv_params'):
        weights = {
            # 'wc1': init_weights([filter_size, filter_size, num_channels, num_filters[0]], name='wc1'), # 5x5 conv, 1 input, 32 outputs
            # 'wc2': init_weights([filter_size, filter_size, num_filters[0], num_filters[1]], name='wc2'), # 5x5 conv, 32 inputs, 64 outputs
            # 'wc3': init_weights([filter_size, filter_size, num_filters[1], num_filters[2]], name='wc3'), # 5x5 conv, 32 inputs, 64 outputs
            'wc1': get_he_weights([filter_size, filter_size, num_channels, num_filters[0]], name='wc1'), # 5x5 conv, 1 input, 32 outputs
            'wc2': get_he_weights([filter_size, filter_size, num_filters[0], num_filters[1]], name='wc2'), # 5x5 conv, 32 inputs, 64 outputs
            'wc3': get_he_weights([filter_size, filter_size, num_filters[1], num_filters[2]], name='wc3'), # 5x5 conv, 32 inputs, 64 outputs
            'wc4': get_he_weights([filter_size, filter_size, num_filters[2], num_filters[3]], name='wc4'),
        }

        biases = {
            'bc1': init_bias([num_filters[0]], name='bc1'),
            'bc2': init_bias([num_filters[1]], name='bc2'),
            'bc3': init_bias([num_filters[2]], name='bc3'),
            'bc4': init_bias([num_filters[3]], name='bc4'),
        }

    fc_inputs = []
    is_training_list = [True, False]
    for is_training in is_training_list:
        conv_layer_0 = norm(conv2d(img=image_input, w=weights['wc1'], b=biases['bc1'], strides=[1,2,2,1]), norm_type=norm_type, decay=decay, conv_id=0, is_training=is_training)
        conv_layer_1 = norm(conv2d(img=conv_layer_0, w=weights['wc2'], b=biases['bc2']), norm_type=norm_type, decay=decay, conv_id=1, is_training=is_training)
        conv_layer_2 = norm(conv2d(img=conv_layer_1, w=weights['wc3'], b=biases['bc3']), norm_type=norm_type, decay=decay, conv_id=2, is_training=is_training)
        conv_layer_3 = norm(conv2d(img=conv_layer_2, w=weights['wc4'], b=biases['bc4']), norm_type=norm_type, decay=decay, conv_id=3, is_training=is_training)
        
        if is_training:
            training_conv_layer_3 = conv_layer_3

        _, num_rows, num_cols, num_fp = conv_layer_3.get_shape()
        num_rows, num_cols, num_fp = [int(x) for x in [num_rows, num_cols, num_fp]]
        x_map = np.empty([num_rows, num_cols], np.float32)
        y_map = np.empty([num_rows, num_cols], np.float32)

        for i in range(num_rows):
            for j in range(num_cols):
                x_map[i, j] = (i - num_rows / 2.0) / num_rows
                y_map[i, j] = (j - num_cols / 2.0) / num_cols

        x_map = tf.convert_to_tensor(x_map)
        y_map = tf.convert_to_tensor(y_map)

        x_map = tf.reshape(x_map, [num_rows * num_cols])
        y_map = tf.reshape(y_map, [num_rows * num_cols])

        # rearrange features to be [batch_size, num_fp, num_rows, num_cols]
        features = tf.reshape(tf.transpose(conv_layer_2, [0,3,1,2]),
                              [-1, num_rows*num_cols])
        softmax = tf.nn.softmax(features)

        fp_x = tf.reduce_sum(tf.multiply(x_map, softmax), [1], keep_dims=True)
        fp_y = tf.reduce_sum(tf.multiply(y_map, softmax), [1], keep_dims=True)

        fp = tf.reshape(tf.concat(axis=1, values=[fp_x, fp_y]), [-1, num_fp*2])

        fc_input = tf.concat(axis=1, values=[fp, state_input]) # TODO - switch these two?
        fc_inputs.append(fc_input)

    fc_output, weights_FC, biases_FC = get_mlp_layers(fc_inputs[0], n_layers, dim_hidden, batch_norm=False, decay=decay, is_training=True)
    test_output, _, _ = get_mlp_layers(fc_inputs[1], n_layers, dim_hidden, batch_norm=False, decay=decay, is_training=False)
    fc_vars = weights_FC + biases_FC
    for i in xrange(n_layers):
        weights['wfc%d' % i] = weights_FC[i]
        weights['bfc%d' % i] = biases_FC[i]
    weights.update(biases)

    loss = euclidean_loss_layer(a=action, b=fc_output, precision=precision, behavior_clone=behavior_clone)
    val_loss = euclidean_loss_layer(a=action, b=test_output, precision=precision, behavior_clone=behavior_clone)
    
    nnet = TfMap.init_from_lists([nn_input, action, precision], [fc_output, test_output], [weights], [loss, val_loss], fp=fp, image=flat_image_input, debug=training_conv_layer_3) #this is training conv layer
    last_conv_vars = fc_inputs[0] #training fc input

    return nnet, fc_vars, last_conv_vars

def conv2d(img, w, b, strides=[1, 1, 1, 1], rate=2, is_dilated=False):
    if is_dilated:
        if strides[1] != 1:
            global CONV_IMG_SIZE
            if img.get_shape().dims[1].value:
                CONV_IMG_SIZE = [int(np.ceil(float(img.get_shape().dims[1].value)/strides[1])), int(np.ceil(float(img.get_shape().dims[2].value)/strides[2]))]
            else:
                CONV_IMG_SIZE = [int(np.ceil(float(CONV_IMG_SIZE[0])/strides[1])), int(np.ceil(float(CONV_IMG_SIZE[1])/strides[2]))]
            img = tf.image.resize_images(img, CONV_IMG_SIZE)
        layer = tf.nn.atrous_conv2d(img, w, rate=rate, padding='SAME') + b
    else:
        layer = tf.nn.conv2d(img, w, strides=strides, padding='SAME') + b
    return layer
    
def conv1d(img, w, b, stride=1):
    layer = tf.nn.conv1d(img, w, stride=stride, padding='SAME') + b
    return layer

def selu(x):
    with ops.name_scope('selu') as scope:
        alpha = 1.6732632423543772848170429916717
        scale = 1.0507009873554804934193349852946
        return scale*tf.where(x>=0.0, x, alpha*tf.nn.elu(x))
        
def lrelu(x, alpha=0.2):
    return tf.where(x>=0.0, x, alpha*tf.nn.relu(x))
        
# def dropout_selu(x, rate, alpha= -1.7580993408473766, fixedPointMean=0.0, fixedPointVar=1.0, 
#                  noise_shape=None, seed=None, name=None, training=False):
#     """Dropout to a value with rescaling."""

#     def dropout_selu_impl(x, rate, alpha, noise_shape, seed, name):
#         keep_prob = 1.0 - rate
#         x = ops.convert_to_tensor(x, name="x")
#         if isinstance(keep_prob, numbers.Real) and not 0 < keep_prob <= 1:
#             raise ValueError("keep_prob must be a scalar tensor or a float in the "
#                                              "range (0, 1], got %g" % keep_prob)
#         keep_prob = ops.convert_to_tensor(keep_prob, dtype=x.dtype, name="keep_prob")
#         keep_prob.get_shape().assert_is_compatible_with(tensor_shape.scalar())

#         alpha = ops.convert_to_tensor(alpha, dtype=x.dtype, name="alpha")
#         keep_prob.get_shape().assert_is_compatible_with(tensor_shape.scalar())

#         if tensor_util.constant_value(keep_prob) == 1:
#             return x

#         noise_shape = noise_shape if noise_shape is not None else array_ops.shape(x)
#         random_tensor = keep_prob
#         random_tensor += random_ops.random_uniform(noise_shape, seed=seed, dtype=x.dtype)
#         binary_tensor = math_ops.floor(random_tensor)
#         ret = x * binary_tensor + alpha * (1-binary_tensor)

#         a = tf.sqrt(fixedPointVar / (keep_prob *((1-keep_prob) * tf.pow(alpha-fixedPointMean,2) + fixedPointVar)))

#         b = fixedPointMean - a * (keep_prob * fixedPointMean + (1 - keep_prob) * alpha)
#         ret = a * ret + b
#         ret.set_shape(x.get_shape())
#         return ret

#     with ops.name_scope(name, "dropout", [x]) as name:
#         return utils.smart_cond(training,
#             lambda: dropout_selu_impl(x, rate, alpha, noise_shape, seed, name),
#             lambda: array_ops.identity(x))
            
def dropout(layer, keep_prob=0.9, is_training=True, name=None, selu=False):
    if selu:
        return dropout_selu(layer, 1.0 - keep_prob, name=name, training=is_training)
    if is_training:
        return tf.nn.dropout(layer, keep_prob=keep_prob, name=name)
    else:
        return tf.add(layer, 0, name=name)

def norm(layer, norm_type='batch_norm', decay=0.9, id=0, is_training=True, activation_fn=tf.nn.relu, prefix='conv_'):
    if norm_type != 'batch_norm' and norm_type != 'layer_norm':
        return tf.nn.relu(layer)
    with tf.variable_scope('norm_layer_%s%d' % (prefix, id)) as vs:
        if norm_type == 'batch_norm':
            if is_training:
                try:
                    layer = tf.contrib.layers.batch_norm(layer, is_training=True, center=True,
                        scale=False, decay=decay, activation_fn=activation_fn, updates_collections=None, scope=vs) # updates_collections=None
                except ValueError:
                    layer = tf.contrib.layers.batch_norm(layer, is_training=True, center=True,
                        scale=False, decay=decay, activation_fn=activation_fn, updates_collections=None, scope=vs, reuse=True) # updates_collections=None
            else:
                layer = tf.contrib.layers.batch_norm(layer, is_training=False, center=True,
                    scale=False, decay=decay, activation_fn=activation_fn, updates_collections=None, scope=vs, reuse=True) # updates_collections=None
        elif norm_type == 'layer_norm': # layer_norm
            # Take activation_fn out to apply lrelu
            try:
                layer = activation_fn(tf.contrib.layers.layer_norm(layer, center=True,
                    scale=False, scope=vs)) # updates_collections=None
                
            except ValueError:
                layer = activation_fn(tf.contrib.layers.layer_norm(layer, center=True,
                    scale=False, scope=vs, reuse=True))
        elif norm_type == 'selu':
            layer = selu(layer)
        else:
            raise NotImplementedError('Other types of norm not implemented.')
        return layer
        
class VBN(object):
    """
    Virtual Batch Normalization
    """

    def __init__(self, x, name, epsilon=1e-5):
        """
        x is the reference batch
        """
        assert isinstance(epsilon, float)

        shape = x.get_shape().as_list()
        with tf.variable_scope(name) as scope:
            self.epsilon = epsilon
            self.name = name
            self.mean = tf.reduce_mean(x, [0, 1, 2], keep_dims=True)
            self.mean_sq = tf.reduce_mean(tf.square(x), [0, 1, 2], keep_dims=True)
            self.batch_size = int(x.get_shape()[0])
            assert x is not None
            assert self.mean is not None
            assert self.mean_sq is not None
            out = tf.nn.relu(self._normalize(x, self.mean, self.mean_sq, "reference"))
            self.reference_output = out

    def __call__(self, x, update=False):
        with tf.variable_scope(self.name) as scope:
            if not update:
                new_coeff = 1. / (self.batch_size + 1.)
                old_coeff = 1. - new_coeff
                new_mean = tf.reduce_mean(x, [1, 2], keep_dims=True)
                new_mean_sq = tf.reduce_mean(tf.square(x), [1, 2], keep_dims=True)
                mean = new_coeff * new_mean + old_coeff * self.mean
                mean_sq = new_coeff * new_mean_sq + old_coeff * self.mean_sq
                out = tf.nn.relu(self._normalize(x, mean, mean_sq, "live"))
            # Update the mean and mean_sq when passing the reference data
            else:
                # Is the implementation correct?
                self.mean = tf.reduce_mean(x, [0, 1, 2], keep_dims=True)
                self.mean_sq = tf.reduce_mean(tf.square(x), [0, 1, 2], keep_dims=True)
                out = tf.nn.relu(self._normalize(x, self.mean, self.mean_sq, "reference"))
            return out

    def _normalize(self, x, mean, mean_sq, message):
        # make sure this is called with a variable scope
        shape = x.get_shape().as_list()
        assert len(shape) == 4
        self.gamma = safe_get("gamma", [shape[-1]],
                                initializer=tf.random_normal_initializer(1., 0.02))
        gamma = tf.reshape(self.gamma, [1, 1, 1, -1])
        self.beta = safe_get("beta", [shape[-1]],
                                initializer=tf.constant_initializer(0.))
        beta = tf.reshape(self.beta, [1, 1, 1, -1])
        assert self.epsilon is not None
        assert mean_sq is not None
        assert mean is not None
        std = tf.sqrt(self.epsilon + mean_sq - tf.square(mean))
        out = x - mean
        out = out / std
        # out = tf.Print(out, [tf.reduce_mean(out, [0, 1, 2]),
        #    tf.reduce_mean(tf.square(out - tf.reduce_mean(out, [0, 1, 2], keep_dims=True)), [0, 1, 2])],
        #    message, first_n=-1)
        out = out * gamma
        out = out + beta
        return out

def max_pool(img, k):
    return tf.nn.max_pool(img, ksize=[1, k, k, 1], strides=[1, k, k, 1], padding='SAME')


# Consider stride size when using xavier for fp network
def get_xavier_weights(filter_shape, poolsize=(2, 2), name=None):
    fan_in = np.prod(filter_shape[1:])
    fan_out = (filter_shape[0] * np.prod(filter_shape[2:]) //
               np.prod(poolsize))

    low = -4*np.sqrt(6.0/(fan_in + fan_out)) # use 4 for sigmoid, 1 for tanh activation
    high = 4*np.sqrt(6.0/(fan_in + fan_out))
    weights = np.random.uniform(low=low, high=high, size=filter_shape)
    return safe_get(name, filter_shape, initializer=tf.constant_initializer(weights))
    # return tf.Variable(tf.random_uniform(filter_shape, minval=low, maxval=high, dtype=tf.float32))

def get_he_weights(filter_shape, name=None):
    fan_in = np.prod(filter_shape[1:])

    stddev = np.sqrt(2.6/fan_in)
    weights = stddev * np.random.randn(filter_shape[0], filter_shape[1], filter_shape[2], filter_shape[3])
    return safe_get(name, filter_shape, initializer=tf.constant_initializer(weights))
