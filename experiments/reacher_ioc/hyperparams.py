from __future__ import division

from datetime import datetime
import os.path
import numpy as np
import operator

from gps import __file__ as gps_filepath
from gps.agent.mjc.agent_mjc import AgentMuJoCo
from gps.algorithm.algorithm_badmm import AlgorithmBADMM
from gps.algorithm.algorithm_traj_opt import AlgorithmTrajOpt
from gps.algorithm.algorithm_mdgps import AlgorithmMDGPS
from gps.algorithm.cost.cost_action import CostAction
from gps.algorithm.cost.cost_ioc_nn import CostIOCNN
from gps.algorithm.cost.cost_state import CostState
from gps.algorithm.cost.cost_fk import CostFK
from gps.algorithm.cost.cost_sum import CostSum
#from gps.algorithm.cost.cost_gym import CostGym
from gps.algorithm.dynamics.dynamics_lr_prior import DynamicsLRPrior
from gps.algorithm.dynamics.dynamics_prior_gmm import DynamicsPriorGMM
from gps.algorithm.traj_opt.traj_opt_lqr_python import TrajOptLQRPython
from gps.algorithm.policy.lin_gauss_init import init_lqr, init_pd, init_demo
from gps.algorithm.policy_opt.policy_opt_caffe import PolicyOptCaffe
from gps.algorithm.policy.policy_prior_gmm import PolicyPriorGMM
from gps.algorithm.cost.cost_utils import RAMP_LINEAR, RAMP_FINAL_ONLY, RAMP_QUADRATIC, evall1l2term
from gps.utility.data_logger import DataLogger

from gps.proto.gps_pb2 import JOINT_ANGLES, JOINT_VELOCITIES, \
        END_EFFECTOR_POINTS, END_EFFECTOR_POINT_VELOCITIES, RGB_IMAGE, RGB_IMAGE_SIZE, ACTION
from gps.gui.config import generate_experiment_info

SENSOR_DIMS = {
    JOINT_ANGLES: 2,
    JOINT_VELOCITIES: 2,
    END_EFFECTOR_POINTS: 6,
    END_EFFECTOR_POINT_VELOCITIES: 6,
    ACTION: 2,
}

BASE_DIR = '/'.join(str.split(__file__, '/')[:-2])
EXP_DIR = '/'.join(str.split(__file__, '/')[:-1]) + '/'
DEMO_DIR = BASE_DIR + '/../experiments/reacher/'

CONDITIONS = 1
DEMO_CONDITIONS = 20

np.random.seed(47)
pos_body_offset = []
pos_body_offset.append(np.array([-0.1, 0.2, 0.0]))
#pos_body_offset.append(np.array([0.05, 0.2, 0.0]))

demo_pos_body_offset = []
#demo_pos_body_offset.append(np.array([-0.1, 0.2, 0.0]))
for _ in range(DEMO_CONDITIONS):
    demo_pos_body_offset.append(np.array([0.4*np.random.rand()-0.3, 0.4*np.random.rand()-0.1 ,0]))

common = {
    'experiment_name': 'my_experiment' + '_' + \
            datetime.strftime(datetime.now(), '%m-%d-%y_%H-%M'),
    'experiment_dir': EXP_DIR,
    'data_files_dir': EXP_DIR + 'data_files/',
    'target_filename': EXP_DIR + 'target.npz',
    'log_filename': EXP_DIR + 'log.txt',
    'demo_exp_dir': DEMO_DIR,
    'demo_controller_file': DEMO_DIR + 'data_files/algorithm_itr_14.pkl',
    'conditions': CONDITIONS,
    'nn_demo': False,
}

if not os.path.exists(common['data_files_dir']):
    os.makedirs(common['data_files_dir'])

agent = {
    'type': AgentMuJoCo,
    'filename': './mjc_models/reacher_img.xml',
    'x0': np.zeros(4),
    'dt': 0.05,
    'substeps': 5,
    'prohibited_ranges_bodypos':[ [None, None, None, None] ],
    'pos_body_offset': pos_body_offset,
    'pos_body_idx': np.array([4]),
    'conditions': common['conditions'],
    'T': 50,
    'sensor_dims': SENSOR_DIMS,
    'state_include': [JOINT_ANGLES, JOINT_VELOCITIES, \
            END_EFFECTOR_POINTS, END_EFFECTOR_POINT_VELOCITIES],
    'obs_include': [JOINT_ANGLES, JOINT_VELOCITIES, \
            END_EFFECTOR_POINTS, END_EFFECTOR_POINT_VELOCITIES],
    'meta_include': [],
    'camera_pos': np.array([0., 0., 3., 0., 0., 0.]),
}

demo_agent = {
    'type': AgentMuJoCo,
    'filename': './mjc_models/reacher_img.xml',
    'x0': np.zeros(4),
    'dt': 0.05,
    'substeps': 5,
    'pos_body_offset': demo_pos_body_offset,
    'pos_body_idx': np.array([4]),
    'conditions': DEMO_CONDITIONS,
    'T': agent['T'],
    'sensor_dims': SENSOR_DIMS,
    'state_include': [JOINT_ANGLES, JOINT_VELOCITIES, \
            END_EFFECTOR_POINTS, END_EFFECTOR_POINT_VELOCITIES],
    'obs_include': [JOINT_ANGLES, JOINT_VELOCITIES, \
            END_EFFECTOR_POINTS, END_EFFECTOR_POINT_VELOCITIES],
    'meta_include': [],
    'camera_pos': np.array([0., 0., 3., 0., 0., 0.]),
}


algorithm = {
    'type': AlgorithmTrajOpt,
    'ioc' : 'ICML',
    'max_ent_traj': 1.0,
    'conditions': common['conditions'],
    'kl_step': 0.5,
    'min_step_mult': 0.05,
    'max_step_mult': 2.0,
    'demo_cond': demo_agent['conditions'],
    'num_demos': 20,
    'demo_var_mult': 1.0,
    'synthetic_cost_samples': 100,
    'iterations': 25,
    'plot_dir': EXP_DIR,
}

PR2_GAINS = np.array([1.0, 1.0])
torque_cost_1 = [{
    'type': CostAction,
    'wu': 1 / PR2_GAINS,
} for i in range(common['conditions'])]

fk_cost_1 = [{
    'type': CostFK,
    'target_end_effector': np.concatenate([np.array([.1, -.1, .01])+ agent['pos_body_offset'][i], np.array([0., 0., 0.])]),
    'wp': np.array([1, 1, 1, 0, 0, 0]),
    'l1': 0.1,
    'l2': 10.0,
    'alpha': 1e-5,
    'evalnorm': evall1l2term,
} for i in range(common['conditions'])]

algorithm['gt_cost'] = [{
    'type': CostSum,
    'costs': [torque_cost_1[i], fk_cost_1[i]],
    'weights': [200.0, 100.0],
}  for i in range(common['conditions'])][0]

algorithm['cost'] = {  # TODO - make vision cost and emp. est derivatives
    'type': CostIOCNN,
    'wu': 200 / PR2_GAINS,
    'T': agent['T'],
    'dO': 16,
    'iterations': 5000,
    'demo_batch_size': 5,
    'sample_batch_size': 5,
    'ioc_loss': algorithm['ioc'],
    'smooth_reg_weight': 1.0,
    'mono_reg_weight': 0.0,
}

#algorithm['init_traj_distr'] = {
#    'type': init_demo,
#    'init_gains':  1.0 / PR2_GAINS,
#    'init_acc': np.zeros(SENSOR_DIMS[ACTION]),
#    'init_var': 5.0,
#    'stiffness': 1.0,
##    'stiffness_vel': 0.5,
#    'final_weight': 50.0,
#    'dt': agent['dt'],
#    'T': agent['T'],
#}

algorithm['init_traj_distr'] = {
    'type': init_lqr,
    'init_gains':  100.0 * np.ones(SENSOR_DIMS[ACTION]),
    'init_acc': np.zeros(SENSOR_DIMS[ACTION]),
    'init_var': 5.0,
    'stiffness': 0.5,
    'stiffness_vel': 0.5,
    'final_weight': 0.5,
    'dt': agent['dt'],
    'T': agent['T'],
}

algorithm['dynamics'] = {
    'type': DynamicsLRPrior,
    'regularization': 1e-6,
    'prior': {
        'type': DynamicsPriorGMM,
        'max_clusters': 30,
        'min_samples_per_cluster': 40,
        'max_samples': 10, #len(common['conditions']),
    },
}

algorithm['traj_opt'] = {
    'type': TrajOptLQRPython,
    'min_eta': 1e-4,
    'max_eta': 1.0,
}


algorithm['policy_prior'] = {
    'type': PolicyPriorGMM,
    'max_clusters': 40,
    'min_samples_per_cluster': 40,
}


config = {
    'iterations': algorithm['iterations'],
    'num_samples': 10,
    'verbose_trials': 5,
    'verbose_policy_trials': 0,
    'common': common,
    'agent': agent,
    'demo_agent': demo_agent,
    'gui_on': True,
    'algorithm': algorithm,
    'conditions': common['conditions'],
    'random_seed': 1,
}

common['info'] = generate_experiment_info(config)