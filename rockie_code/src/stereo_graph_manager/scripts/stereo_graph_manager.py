#!/usr/bin/python
'''
This node subscribes to the stereo image historian node, and every time a stereo image
pair is saved it will perform feature matching on it. It will then save features to the
database
'''
import numpy as np
import cv2
import math
import cv
import os
import rospy
from std_msgs.msg import String
from sensor_msgs.msg import Image as ros_image
from cv_bridge import CvBridge, CvBridgeError
from stereo_feature_identifier_db import Stereo_Pair_Keypoints, Stereo_Pair_Keypoint_Matches, Stereo_3D_Matches, Graph_Nodes, Graph_Edges
from stereo_historian_db import Stereo_Image_Pair, Base
import datetime
import cPickle as pickle
from sqlalchemy import create_engine
import random
from sqlalchemy.orm import sessionmaker

engine = create_engine('mysql://root@localhost/rockie')
Base.metadata.bind = engine
DBSession = sessionmaker(bind=engine)
session = DBSession()

stereo_historian_topic = '/my_stereo/stereo_image_saves'
stereo_feature_identifier_topic = '/my_stereo/stereo_image_keypoint_saves'
stereo_feature_matcher_topic = '/my_stereo/stereo_image_keypoint_matches'
stereo_feature_triangulator_topic = '/my_stereo/stereo_image_3D_points'
stereo_graph_manager_topic = '/my_stereo/stereo_graph_node_updates'

# FLANN parameters
FLANN_INDEX_KDTREE = 0
index_params = dict(algorithm = FLANN_INDEX_KDTREE, trees = 5)
search_params = dict(checks=50)   # or pass empty dictionary

flann = cv2.FlannBasedMatcher(index_params,search_params)

#if we can match 70% of the 3D points, don't create a new feature node
new_feature_threshold = .7

#If we have at least 7 matches, make a connection
new_connection_threshold = 7 

stereo_imagepath_base = "{0}/Code/wpi-sample-return-robot-challenge/rockie_code/src/stereo_historian/scripts/images/left/".format(os.getenv("HOME"))

def update_graph_callback(_3d_matches_data_id):
  global session
  global DBSession
  global pub
  
  _3d_matches = get_3d_points(_3d_matches_data_id.data)

  if(_3d_matches != None):

    stereo_keypoint_matches = get_sp_keypoint_matches(_3d_matches.sp_matches_id)
    stereo_keypoints = get_stereo_pair_keypoint(stereo_keypoint_matches.stereo_pair_keypoint_id)
    stereo_image_pair = get_stereo_image_pair(stereo_keypoints.stereo_image_pair_id)

    update_graph_slam(_3d_matches, stereo_image_pair)

  #create pose node
  #connect pose node to previous pose node
  #use wheel odometry to get edge transform for this new edge

  #iterate through prior 3d_matches
  #if there is a 3d_match-3d_match (> min num of matches):
    #connect newly created pose node to found 3d_match

    #if we've found a 3d_match-3d_match match that share enough nodes
    #don't add new 3d_matches as new feature node
    #else
    #add new 3d_matches as new feature node to graph
    #connect this new node to new pose node

  ################For more info, look at Thrun's graphslam paper http://robots.stanford.edu/papers/thrun.graphslam.pdf############

  #Every pose-feature-pose edgewise connection can be reduced to a pose-pose edge (constraint)
  #NOTE: Don't reduce a pose-wpi-feature-node-pose edges to a pose-pose edge, this will
  #discard global info

  #We can then reduce our pose-pose/pose-feature graph to just a pose-pose graph
  #A rigid body transformation (rotation followed by translation) defines this constraint

  #The constraint error is defined as the square distance between each 3d point match after transformation

  #Once this is done we can at least recover odometry (pose-pose constraints describe motion between poses)

  #To recover absolute position, we first optimize our pose-pose constraints (least-squares/libg20)
  #then, we find a wpi feature node in the graph. all pose-nodes that have an edge to this feature
  #node(i.e. have seen the wpi feature) are given an absolute/global location.

  #From these nodes, we propogate out the optimized transformations between pose-pose-nodes
  #we should be able to just find a path through the graph to our current position, and apply the
  #transformation along the path to recover the global position

def get_stereo_image_pair(stereo_image_pair_id):
  global session

  query = session.query(Stereo_Image_Pair)
  stereo_image_pair = query.filter_by(stereo_image_pair_id = int(stereo_image_pair_id)).first()
  return stereo_image_pair

def get_stereo_pair_keypoint(stereo_pair_keypoint_id):
  global session
  query = session.query(Stereo_Pair_Keypoints)
  stereo_pair_keypoint = query.filter_by(stereo_pair_keypoint_id = int(stereo_pair_keypoint_id)).first()
  return stereo_pair_keypoint

def get_sp_keypoint_matches(sp_keypoint_matches_id):
  global session
  query = session.query(Stereo_Pair_Keypoint_Matches)
  sp_keypoint_matches = query.filter_by(sp_keypoint_matches_id = int(sp_keypoint_matches_id)).first()
  return sp_keypoint_matches

def get_3d_matches_object(_3d_matches):
  obj = pickle.load(open(_3d_matches.sp_3d_matches_filepath))

  #obj[0] = np.array(obj[0])
  #obj[1] = np.array(obj[1])

  return obj

def update_slam_graph(_3d_matches, stereo_image_pair):

  _3d_matches_object = get_3d_matches_object(_3d_matches)

  [new_point_descs, new_point_positions] = _3d_matches_object

  new_point_descs = np.array(new_point_descs, np.float32)
  new_point_positions = np.array(new_point_positions, np.int32)

  #TODO: Connect poses with wheel odometry
  new_pose_node = create_pose_node(stereo_image_pair.stereo_image_pair_id)  
  #connect_poses(new_pose_node.node_id, new_pose_node.node_id - 1)

  all_feature_nodes = get_all_feature_nodes()

  insert_new_feature_node = True

  for feature_node in all_feature_nodes:

    feature_node_3d_points = get_3d_points(feature_node.sp_3d_matches_id)
    feature_node_3d_points_obj = get_3d_matches_object(feature_node_3d_points)

    [fn_descs, fn_positions] = feature_node_3d_points_obj

    feature_point_descs = np.array(fn_descs, np.float32)
    feature_point_positions = np.array(fn_positions, np.int32)

    point_matches = get_3d_point_matches(new_point_descs, feature_point_descs)

    num_3d_matches = len(point_matches)
    num_feature_node_points = feature_point_positions.shape[0]

    #Returns true if we have enough matches to connect new pose to existing feature, false otherwise
    if(num_3d_matches > new_connection_threshold):
      transform = calculate_3d_transform(point_matches, new_point_positions, feature_point_positions)
      connect_pose_to_feature(new_pose_node, feature_node, transform, point_matches, new_point_positions, feature_point_positions)

      #don't add new feature node if num of matches is relatively large
      if(num_3d_matches > new_feature_threshold*num_feature_node_points):
        insert_new_feature_node = False

  if(insert_new_feature_node):
    new_feature_node = add_new_feature_node(_3d_matches)
    identity_transform = get_identity_transform()
    connect_pose_to_feature(new_pose_node, new_feature_node, identity_transform, None, new_point_positions, new_point_positions)

def get_identity_transform():
  return [np.empty([3, 3]).tolist(), np.empty([1, 3]).tolist()]

def add_new_feature_node(_3d_matches):
  global session
  feature_node = Graph_Nodes()
  feature_node.node_type = 'feature'
  feature_node.sp_3d_matches_id = _3d_matches.sp_3d_matches_id

  session.add(feature_node)
  session.commit()

#Look at github wiki  
def calculate_3d_transform(matches, positions_1, positions_2):
  centroid_1 = get_centroid(positions_1)
  centroid_2 = get_centroid(positions_2)

  t = centroid_2 - centroid_1

  H = get_covariance_matrix(matches, positions_1, positions_2, centroid_1, centroid_2)

  [U, S, V] = np.linalg.svd(H, full_matrices=True)

  R = V*U.T

  print("R = {0}".format(R))
  print("t = {0}".format(t))

  return [R.tolist(), t.tolist()]

def get_centroid(positions):
  num_points = positions.shape[0]
  summed_points = np.sum(positions, axis=0)
  return np.divide(summed_points, num_points)

def get_covariance_matrix(matches, positions_1, positions_2, centroid_1, centroid_2):
  H = np.empty([3, 3])

  for match in matches:
    point_1 = positions_1[match.queryIdx, :]
    point_2 = positions_2[match.trainIdx, :]

    H += (point_1 - centroid_1)*(point_2 - centroid_2)

  return H

def connect_pose_to_feature(new_pose_node, feature_node, transform, point_matches, pose_3d_points, feature_3d_points):
  global session

  edge = Graph_Edges()
  edge.node_1_id = new_pose_node.node_id
  edge.node_1_type = 'pose'
  edge.node_2_id = feature_node.node_id
  edge.node_2_type = 'feature'

  #transform_filepath = save_transform(transform)
  #edge.optimal_transform_filepath = transform_filepath

  _3d_matches_filepath = save_3d_matches(point_matches,
      pose_3d_points, 
      feature_3d_points, 
      new_pose_node.node_id, 
      feature_node.node_id, 
      transform)

  edge._3d_matches_filepath = _3d_matches_filepath

  session.add(edge)
  session.commit()

class py_match:
  pass

def convert_match(match):
  new_match = py_match()
  new_match.distance = match.distance
  new_match.imgIdx = match.imgIdx
  new_match.queryIdx = match.queryIdx
  new_match.trainIdx = match.trainIdx

  return new_match

def save_3d_matches(point_matches, pose_3d_points, feature_3d_points, pose_id, feature_id, transform):
  if(point_matches != None):
    matches = [convert_match(match) for match in point_matches]

  _3d_matches = [matches, pose_3d_points.tolist(), feature_3d_points.tolist(), transform]

  filepath = "{0}node_{1}_to_node_{2}_edge.edge".format(stereo_imagepath_base, pose_id, feature_id)
  pickle.dump(_3d_matches, open(filepath, 'wb'))
  return filepath

def create_pose_node(stereo_image_pair_id):
  global session
  node = Graph_Nodes()
  node.stereo_image_pair_id = stereo_image_pair_id
  node.node_type = 'pose'

  session.add(node)
  session.commit()
  return node

def get_keypoints_pair(spk_id):
  global session
  query = session.query(Stereo_Pair_Keypoints)
  stereo_pair_keypoints = query.filter_by(stereo_pair_keypoint_id = int(spk_id)).first()
  return stereo_pair_keypoints

def get_3d_points(_3d_matches_id):
  global session
  query = session.query(Stereo_3D_Matches)
  _3d_matches = query.filter_by(sp_3d_matches_id = _3d_matches_id).first()
  return _3d_matches

def get_3d_point_matches(descs1, descs2):
  global flann

  #[descs1, positions1] = _3d_points_1
  #[descs2, positions2] = _3d_points_2

  matches = flann.match(descs1, descs2)

  #get descriptor matches for 3d_matches_1 and 3d_matches_2
  #match
  #return matches

  #[descs1, kpts1] = get_3d_points_keypoints(_3d_matches_1)
  #[descs2, kpts2] = get_3d_points_keypoints(_3d_matches_2)
  
  return matches

def get_all_feature_nodes():
  global session
  query = session.query(Graph_Nodes)
  all_feature_nodes = query.filter(Graph_Nodes.node_type == 'feature')
  return all_feature_nodes

def triangulate(matches, kpts1, kpts2):

  _3d_points = []

  for match in matches:  

    query_pt = kpts1[match.queryIdx]
    train_pt = kpts2[match.trainIdx]

    #should check against epipolar line    
    if(math.fabs(query_pt.pt[1] - train_pt.pt[1]) > 10):
      continue

    disparity = math.fabs(query_pt.pt[0] - train_pt.pt[0])

    #consider query image as origin
    x = query_pt.pt[0]
    y = query_pt.pt[1]

    #see pg 371 and 416 of O'REILLY "Learning OpenCV" 2008
    Z = (f*camera_dist)/disparity
    Y = (y*Z)/f
    X = (x*Z)/f

    _3d_points.append([match, X, Y, Z])

  return _3d_points

def store_3d_points(_3d_points, matches_filepath):
  filepath = "{0}.3d_points".format(matches_filepath)
  pickle.dump(_3d_points, open(filepath, 'wb'))
  return filepath

class py_match:
  pass

def convert_match(match):
  new_match = py_match()
  new_match.distance = match.distance
  new_match.imgIdx = match.imgIdx
  new_match.queryIdx = match.queryIdx
  new_match.trainIdx = match.trainIdx

  return new_match

def save_keypoint_matches(matches, stereo_pair_keypoint):
  
  filepath = "{0}.keypoint_matches".format(stereo_pair_keypoint.left_keypoints_filepath)
  
  matches = [convert_match(match) for match in matches]

  pickle.dump(matches, open(filepath, 'wb'))
  return filepath

def recreate_keypoints(kp):
  return cv2.KeyPoint(x=kp.pt[0], y=kp.pt[1], _size=kp.size, _angle=kp.angle, _response=kp.response, _octave=kp.octave, _class_id=kp.class_id) 

def get_left_keypoints(stereo_pair_keypoint):
  left_keypoints_filepath = "{0}".format(stereo_pair_keypoint.left_keypoints_filepath)

  kpts_descs = pickle.load(open(left_keypoints_filepath, "rb"))
  kpts_descs[0] = [recreate_keypoints(kp) for kp in kpts_descs[0]]
  return kpts_descs

def get_right_keypoints(stereo_pair_keypoint):
  right_keypoints_filepath = "{0}".format(stereo_pair_keypoint.right_keypoints_filepath)

  return pickle.load(open(right_keypoints_filepath, "rb"))

def get_sp_keypoint_matches(sp_keypoint_matches_id):
  global session
  query = session.query(Stereo_Pair_Keypoint_Matches)
  sp_keypoint_matches = query.filter_by(sp_keypoint_matches_id = int(sp_keypoint_matches_id)).first()
  return sp_keypoint_matches

def update_graph():
  rospy.init_node("stereo_graph_manager")
  rospy.Subscriber(stereo_feature_triangulator_topic, String, update_graph_callback)
  rospy.spin()

class SerializableKeypoint():
  pass

if __name__ == '__main__':
  update_graph()

