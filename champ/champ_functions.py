from __future__ import absolute_import
from future.utils import iteritems,iterkeys
from future.utils import lmap

from multiprocessing import Pool
from collections import defaultdict, Hashable
from contextlib import contextmanager
import numpy as np
from numpy.random import choice, uniform
from scipy.spatial import HalfspaceIntersection
from scipy.optimize import linprog
import warnings

@contextmanager
def terminating(obj):
    '''
    Context manager to handle appropriate shutdown of processes
    :param obj: obj to open
    :return:
    '''
    try:
        yield obj
    finally:
        obj.terminate()

def create_coefarray_from_partitions(partition_array, A_mat, P_mat, C_mat=None,nprocesses=0):
    '''
   :param partition_array: Each row is one of M partitions of the network with N nodes.  Community labels must be hashable.
   :param A_mat: Interlayer (single layer) adjacency matrix
   :param P_mat: Matrix representing null model of connectivity (i.e configuration model - :math:`\\frac{k_ik_j}{2m}`
   :param C_mat: Optional matrix representing interlayer connectivity
   :param nprocesses: Optional number of processes to use (0 or 1 for single core)
   :type nprocesses: int
   :return: size :math:`M\\times\\text{Dim}` array of coefficients for each partition. Dim can be 2 (single layer) \
   or 3 (multilayer)

    '''
    outarray = []

    if nprocesses==0 or nprocesses==1:


        for partition in partition_array:
            curarray=[]
            curarray.append(calculate_coefficient(partition,A_mat))
            curarray.append(calculate_coefficient(partition,P_mat))
            curarray.append(calculate_coefficient(partition,C_mat))
            outarray.append(curarray)

    else:


        parallel_args=[]
        for partition in partition_array:
            parallel_args.append((partition, A_mat))
            parallel_args.append((partition, P_mat))
            parallel_args.append((partition, C_mat))
        #map preserves order
        with terminating(Pool(processes=nprocesses)) as pool:
            parallel_res=pool.map(_calculate_coefficient_parallel,parallel_args)
        outarray=np.array(parallel_res).reshape((3,len(parallel_res)/3))
    return np.array(outarray)

def create_halfspaces_from_array(coef_array):
    '''
    create a list of halfspaces from an array of coefficent.  Each half space is defined by\
     the inequality\:
    :math:`normal\\dot point + offset \\le 0`

    Where each row represents the coefficients for a particular partition.
    For single Layer network, omit C_i's.

    :return: list of halfspaces.
    '''

    singlelayer = False
    if coef_array.shape[1] == 2:
        singlelayer = True

    cconsts = coef_array[:, 0]
    cgammas = coef_array[:, 1]
    if not singlelayer:
        comegas = coef_array[:, 2]

    if singlelayer:
        nvs = np.vstack((cgammas, np.ones(coef_array.shape[0])))
        pts = np.vstack((np.zeros(coef_array.shape[0]), cconsts))
    else:
        nvs = np.vstack((cgammas, -comegas, np.ones(coef_array.shape[0])))
        pts = np.vstack((np.zeros(coef_array.shape[0]), np.zeros(coef_array.shape[0]), cconsts))

    nvs = nvs / np.linalg.norm(nvs, axis=0)
    offs = np.sum(nvs * pts, axis=0)  # dot product on each column

    # array of shape (number of halfspaces, dimension+1)
    # Each row represents a halfspace by [normal; offset]
    # I.e. Ax + b <= 0 is represented by [A; b]
    return np.vstack((-nvs, offs)).T

def sort_points(points):
    '''

    :param points:
    :return:
    '''
    if len(points[0])>2:
        cent = (sum([p[0] for p in points]) / len(points), sum([p[1] for p in points]) / len(points))
        points.sort(key=lambda x: np.arctan2(x[1] - cent[1], x[0] - cent[0]))
    else:
       points.sort(key=lambda x: x[0]) #just sort along x-axis


    return points

def get_interior_point(hs_list):
    '''
    Find interior point to calculate intersections
    :param hs_list: list of halfspaces
    :return: an approximation to the point most interior to the halfspace intersection polyhedron (Chebyshev center) if
    this computation succeeds. Otherwise, a point a small step towards the interior from the first plane in hs_list.
    '''

    normals, offsets = np.split(hs_list, [-1], axis=1)
    # in our case, the last four halfspaces may be boundary halfspaces
    interior_hs, boundaries = np.split(hs_list, [-4], axis=0)

    # randomly sample up to 50 of the halfspaces
    sample_len = min(50, len(interior_hs))
    sampled_hs = np.vstack((interior_hs[choice(interior_hs.shape[0], sample_len, replace=False)], boundaries))

    # compute the Chebyshev center of the sampled halfspaces' intersection
    norm_vector = np.reshape(np.linalg.norm(sampled_hs[:, :-1], axis=1), (sampled_hs.shape[0], 1))
    c = np.zeros((sampled_hs.shape[1],))
    c[-1] = -1
    A = np.hstack((sampled_hs[:, :-1], norm_vector))
    b = -sampled_hs[:, -1:]

    try:
        res = linprog(c, A_ub=A, b_ub=b)
        intpt = res.x[:-1]  # res.x contains [interior_point, distance to enclosing polyhedron]

        # ensure that the computed point is actually interior to all halfspaces
        if (np.dot(normals, intpt) + np.transpose(offsets) < 0).all() and res.success:
            return intpt
    except MemoryError:
        # with hundreds of thousands of halfspaces, linprog fails to allocate initial memory
        warnings.warn("Interior point calculation: scipy.optimize.linprog ran out of memory.", RuntimeWarning)

    warnings.warn("Interior point calculation: falling back to 'small step' approach.", RuntimeWarning)

    z_vals = [-1.0 * offset / normal[-1] for normal, offset in zip(normals, offsets) if
              np.abs(normal[-1]) > np.power(10.0, -15)]

    # take a small step into interior from 1st plane.
    dim = hs_list.shape[1] - 1  # hs_list has shape (number of halfspaces, dimension+1)
    intpt = np.array([0 for _ in range(dim - 1)] + [np.max(z_vals)])
    internal_step = np.array([.000001 for _ in range(dim)])
    return intpt + internal_step




def calculate_coefficient(com_vec,adj_matrix):
    '''
    For a given connection matrix and set of community labels, calculate the coeffcient
    for plane/line associated with that connectivity matrix

    :param com_vec: list or vector with community membership for each element of network
    ordered the same as the rows/col of adj_matrix
    :param adj_matrix: adjacency matrix for connections to calculate coefficients for
    (i.e. A_ij, P_ij, C_ij, etc..) ordered the same as com_vec
    :return:

    '''

    com_inddict = {}

    allcoms = sorted(list(set(com_vec)))
    assert com_vec.shape[0] == adj_matrix.shape[0]
    sumA = 0

    # store indices for each community together in dict
    for i, val in enumerate(com_vec):
        try:
            com_inddict[val] = com_inddict.get(val, []) + [i]
        except TypeError:
            raise TypeError ("Community labels must be hashable- isinstance(%s,Hashable): " %(str(val)),\
                             isinstance(val,Hashable))

    # convert indices to np_array
    for k, val in iteritems(com_inddict):
        com_inddict[k] = np.array(val)

    for com in allcoms:
        cind = com_inddict[com]
        if cind.shape[0] == 1:  # throws type error if try to index with scalar
            sumA += np.sum(adj_matrix[cind, cind])
        else:
            sumA += np.sum(adj_matrix[np.ix_(cind, cind)])

    return sumA

def _calculate_coefficient_parallel(comvec_mat):
    '''
    wrapper function for calc coefficient with single parameter for use with the \
    multiprocessing map call

    :param comvec_mat: (community vector, adj_matrix
    :return: calculate_coefficient to get coeficient
    '''
    com_vec,adj_matrix=comvec_mat

    return calculate_coefficient(com_vec,adj_matrix)

def comp_points(pt1,pt2):
    '''
    check for equality within certain tolerance
    :param pt1:
    :param pt2:
    :return:

    '''
    for i in range(len(pt1)):
        if np.abs(pt1[i]-pt2[i])>np.power(10.0,-15):
            return False

    return True


def get_intersection(coef_array, max_pt=None):
    '''
    Calculate the intersection of the halfspaces (planes) that form the convex hull

   :param coef_array: NxM array of M coefficients across each row representing N partitions
   :type coef_array: array
   :param max_pt: Upper bound for the domains (in the xy plane). This will restrict the convex hull \
    to be within the specified range of gamma/omega (such as the range of parameters originally searched using Louvain).
   :type max_pt: (float,float) or float
   :return: dictionary mapping the index of the elements in the convex hull to the points defining the boundary
    of the domain
    '''

    halfspaces = create_halfspaces_from_array(coef_array)
    num_input_halfspaces = len(halfspaces)

    singlelayer = False
    if halfspaces.shape[1] - 1 == 2:  # 2D case, halfspaces.shape is (number of halfspaces, dimension+1)
        singlelayer = True

    # Create Boundary Halfspaces - These will always be included in the convex hull
    # and need to be removed before returning dictionary

    boundary_halfspaces = []
    if not singlelayer:
        # origin boundaries
        boundary_halfspaces.extend([np.array([0, -1.0, 0, 0]), np.array([-1.0, 0, 0, 0])])
        if max_pt is not None:
            boundary_halfspaces.extend([np.array([0, 1.0, 0, -1.0 * max_pt[0]]),
                                        np.array([1.0, 0, 0, -1.0 * max_pt[1]])])
    else:
        boundary_halfspaces.extend([np.array([-1.0, 0, 0]),  # y-axis
                                    np.array([0, -1.0, 0])])  # x-axis
        if max_pt is not None:
            boundary_halfspaces.append(np.array([1.0, 0, -1.0 * max_pt]))

    # We expect infinite vertices in the halfspace intersection, so we can ignore numpy's floating point warnings
    old_settings = np.seterr(divide='ignore', invalid='ignore')

    halfspaces = np.vstack((halfspaces, ) + tuple(boundary_halfspaces))

    if max_pt is None:
        if not singlelayer:
            # in this case, we will calculate max boundary planes later, so we'll impose x, y <= 10.0
            # for the interior point calculation here.
            interior_pt = get_interior_point(np.vstack((halfspaces,) +
                                                       (np.array([0, 1.0, 0, -10.0]), np.array([1.0, 0, 0, -10.0]))))
        else:
            # similarly, in the 2D case, we impose x <= 10.0 for the interior point calculation
            interior_pt = get_interior_point(np.vstack((halfspaces,) + (np.array([1.0, 0, -10.0]),)))
    else:
        interior_pt = get_interior_point(halfspaces)

    # Find boundary intersection of half spaces
    hs_inter = HalfspaceIntersection(halfspaces, interior_pt)

    non_inf_vert = np.array([v for v in hs_inter.intersections if np.isfinite(v[0])])
    mx = np.max(non_inf_vert, axis=0)

    # max intersection on y-axis (x=0) implies there are no intersections in gamma direction.
    if np.abs(mx[0]) < np.power(10.0, -15) and np.abs(mx[1]) < np.power(10.0, -15):
        raise ValueError("Max intersection detected at (0,0).  Invalid input set.")

    if np.abs(mx[1]) < np.power(10.0, -15):
        mx[1] = mx[0]
    if np.abs(mx[0]) < np.power(10.0, -15):
        mx[0] = mx[1]

    # At this point we include max boundary planes and recalculate the intersection
    # to correct inf points.  We only do this for single layer
    if max_pt is None:
        if not singlelayer:
            boundary_halfspaces.extend([np.array([0, 1.0, 0, -1.0 * mx[1]]),
                                        np.array([1.0, 0, 0, -1.0 * mx[0]])])
            halfspaces = np.vstack((halfspaces, ) + tuple(boundary_halfspaces[-2:]))

    if not singlelayer:
        # Find boundary intersection of half spaces
        interior_pt = get_interior_point(halfspaces)
        hs_inter = HalfspaceIntersection(halfspaces, interior_pt)

    # revert numpy floating point warnings
    np.seterr(**old_settings)

    # scipy does not support facets by halfspace directly, so we must compute them
    facets_by_halfspace = defaultdict(list)
    for v, idx in zip(hs_inter.intersections, hs_inter.dual_facets):
        if np.isfinite(v).all():
            for i in idx:
                facets_by_halfspace[i].append(v)

    ind_2_domain = {}
    dimension = 2 if singlelayer else 3

    for i, vlist in facets_by_halfspace.items():
        # Empty domains
        if len(vlist) == 0:
            continue

        # these are the boundary planes appended on end
        if not i < num_input_halfspaces:
            continue

        pts = sort_points(vlist)
        pt2rm = []
        for j in range(len(pts) - 1):
            if comp_points(pts[j], pts[j + 1]):
                pt2rm.append(j)
        pt2rm.reverse()
        for j in pt2rm:
            pts.pop(j)
        if len(pts) >= dimension:  # must be at least 2 pts in 2D, 3 pt in 3D, etc.
            ind_2_domain[i] = pts

    # use non-inf vertices to return
    return ind_2_domain


def _random_plane():

    normal = np.array([uniform(-0.5, 0.5), uniform(-0.5, 0.5), -1])
    normal /= np.linalg.norm(normal)
    min_offset = -min(0,
                      normal[0],
                      normal[1],
                      normal[0] + normal[1])
    max_offset = -max(normal[2],
                      normal[2] + normal[0],
                      normal[2] + normal[1],
                      normal[2] + normal[0] + normal[1])
    # the 0.25 and 0.75 factors here just force more intersections
    offset = uniform(0.75 * min_offset + 0.25 * max_offset,
                     0.25 * min_offset + 0.75 * max_offset)

    #Return a coefficient representation instead
    return np.array([normal[0],normal[1],-1*offset/normal[2]])
    # return hs.Halfspace(normal, offset)

def _random_line():
    '''
    generate a random line in gamma,Q plane
    :return:
    '''
    # normal = np.array([uniform(.5, 2),-1])
    # normal /= np.linalg.norm(normal)

    #just sample slope and intercept directly
    slope=uniform(1/5.0,5)
    inter=uniform(0,2)

    # offset=-1.0*normal.dot(pt)

    # the 0.25 and 0.75 factors here just force more intersections
    # Return a coefficient representation instead
    return np.array([inter, slope])

def get_random_halfspaces(n=100,dim=3):
    '''Generate random halfspaces for testing
    :param n: number of halfspaces to return (default=100)
    '''
    test_hs=[]
    for _ in range(n):
        if dim==3:
            test_hs.append(_random_plane())
        elif dim==2:
            test_hs.append(_random_line())
        else:
            raise NotImplementedError("Only 2D or 3D Random Halfspaces implemented")
    return np.array(test_hs)
    # return test_hs