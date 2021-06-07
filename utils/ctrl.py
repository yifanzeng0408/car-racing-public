import datetime
import numpy as np
import casadi as ca
from utils import lmpc_helper
from casadi import *
from scipy import sparse
from scipy.sparse import vstack


def pid(xcurv, xtarget, udim):
    start_timer = datetime.datetime.now()
    u_next = np.zeros(udim)
    vt = xtarget[0]
    eyt = xtarget[5]
    u_next[0] = (-0.6 * (xcurv[5] - eyt) - 0.9 * xcurv[3])
    u_next[1] = 1.5 * (vt - xcurv[0])
    end_timer = datetime.datetime.now()
    solver_time = (end_timer - start_timer).total_seconds()
    print("solver time: {}".format(solver_time))
    return u_next


def mpc(xcurv, xtarget, udim, mpc_lti_param):
    vt = xtarget[0]
    eyt = xtarget[5]
    start_timer = datetime.datetime.now()
    opti = ca.Opti()
    # define variables
    xvar = opti.variable(len(xcurv), mpc_lti_param.num_of_horizon + 1)
    uvar = opti.variable(udim, mpc_lti_param.num_of_horizon)
    cost = 0
    opti.subject_to(xvar[:, 0] == xcurv)
    # dynamics + state/input constraints
    for i in range(mpc_lti_param.num_of_horizon):
        # system dynamics
        opti.subject_to(
            xvar[:, i + 1] == ca.mtimes(mpc_lti_param.matrix_A, xvar[:, i]) +
            ca.mtimes(mpc_lti_param.matrix_B, uvar[:, i])
        )
        # min and max of delta
        opti.subject_to(-0.5 <= uvar[0, i])
        opti.subject_to(uvar[0, i] <= 0.5)
        # min and max of a
        opti.subject_to(-1.0 <= uvar[1, i])
        opti.subject_to(uvar[1, i] <= 1.0)
        # input cost
        cost += ca.mtimes(uvar[:, i].T,
                          ca.mtimes(mpc_lti_param.matrix_R, uvar[:, i]))
    for i in range(mpc_lti_param.num_of_horizon + 1):
        # speed vx upper bound
        opti.subject_to(xvar[0, i] <= 10.0)
        opti.subject_to(xvar[0, i] >= 0.0)
        # min and max of ey
        opti.subject_to(xvar[5, i] <= 2.0)
        opti.subject_to(-2.0 <= xvar[5, i])
        # state cost
        cost += ca.mtimes((xvar[:, i] - xtarget).T,
                          ca.mtimes(mpc_lti_param.matrix_Q, xvar[:, i] - xtarget))
    # setup solver
    option = {"verbose": False, "ipopt.print_level": 0, "print_time": 0}
    opti.minimize(cost)
    opti.solver("ipopt", option)
    sol = opti.solve()
    end_timer = datetime.datetime.now()
    solver_time = (end_timer - start_timer).total_seconds()
    print("solver time: {}".format(solver_time))
    x_pred = sol.value(xvar).T
    u_pred = sol.value(uvar).T
    return u_pred[0, :]


def mpccbf(xcurv, xtarget, udim, vehicles, agent_name, lap_length, time, timestep, realtime_flag, mpc_cbf_param):
    vt = xtarget[0]
    eyt = xtarget[5]
    start_timer = datetime.datetime.now()
    opti = ca.Opti()
    # define variables
    xvar = opti.variable(len(xcurv), mpc_cbf_param.num_of_horizon + 1)
    uvar = opti.variable(udim, mpc_cbf_param.num_of_horizon)
    cost = 0
    opti.subject_to(xvar[:, 0] == xcurv)
    # get other vehicles' state estimations
    safety_time = 2.0
    dist_margin_front = xcurv[0] * safety_time
    dist_margin_behind = xcurv[0] * safety_time
    num_cycle_ego = int(xcurv[4] / lap_length)
    dist_ego = xcurv[4] - num_cycle_ego * lap_length
    obs_infos = {}
    for name in list(vehicles):
        if name != agent_name:
            # get predictions from other vehicles
            if realtime_flag == False:
                obs_traj, _ = vehicles[name].get_trajectory_nsteps(
                    time, timestep, mpc_cbf_param.num_of_horizon + 1
                )
            elif realtime_flag == True:
                obs_traj, _ = vehicles[name].get_trajectory_nsteps(
                    mpc_cbf_param.num_of_horizon + 1)
            else:
                pass
            # check whether the obstacle is nearby, not consider it if not
            num_cycle_obs = int(obs_traj[4, 0] / lap_length)
            dist_obs = obs_traj[4, 0] - num_cycle_obs * lap_length
            if (dist_ego > dist_obs - dist_margin_front) & (dist_ego < dist_obs + dist_margin_behind):
                obs_infos[name] = obs_traj
    # slack variables for control barrier functions
    cbf_slack = opti.variable(len(obs_infos), mpc_cbf_param.num_of_horizon + 1)
    # obstacle avoidance
    safety_margin = 0.15
    degree = 6  # 2, 4, 6, 8
    for count, obs_name in enumerate(obs_infos):
        obs_traj = obs_infos[obs_name]
        # get ego agent and obstacles' dimensions
        l_agent = vehicles[agent_name].param.length / 2
        w_agent = vehicles[agent_name].param.width / 2
        l_obs = vehicles[obs_name].param.length / 2
        w_obs = vehicles[obs_name].param.width / 2
        # calculate control barrier functions for each obstacle at timestep
        for i in range(mpc_cbf_param.num_of_horizon):
            num_cycle_obs = int(obs_traj[4, 0] / lap_length)
            diffs = xvar[4, i] - obs_traj[4, i] - \
                (num_cycle_ego - num_cycle_obs) * lap_length
            diffey = xvar[5, i] - obs_traj[5, i]
            diffs_next = xvar[4, i + 1] - obs_traj[4, i + 1]
            diffey_next = xvar[5, i + 1] - obs_traj[5, i + 1]
            h = (
                diffs ** degree / ((l_agent + l_obs) ** degree)
                + diffey ** degree / ((w_agent + w_obs) ** degree)
                - 1
                - safety_margin
                - cbf_slack[count, i]
            )
            h_next = (
                diffs_next ** degree / ((l_agent + l_obs) ** degree)
                + diffey_next ** degree / ((w_agent + w_obs) ** degree)
                - 1
                - safety_margin
                - cbf_slack[count, i + 1]
            )
            opti.subject_to(h_next - h >= -mpc_cbf_param.alpha * h)
            opti.subject_to(cbf_slack[count, i] >= 0)
            cost += 10000 * cbf_slack[count, i]
        opti.subject_to(cbf_slack[count, i + 1] >= 0)
        cost += 10000 * cbf_slack[count, i + 1]
    # dynamics + state/input constraints
    for i in range(mpc_cbf_param.num_of_horizon):
        # system dynamics
        opti.subject_to(
            xvar[:, i + 1] == ca.mtimes(mpc_cbf_param.matrix_A, xvar[:, i]) +
            ca.mtimes(mpc_cbf_param.matrix_B, uvar[:, i])
        )
        # min and max of delta
        opti.subject_to(-0.5 <= uvar[0, i])
        opti.subject_to(uvar[0, i] <= 0.5)
        # min and max of a
        opti.subject_to(-1.0 <= uvar[1, i])
        opti.subject_to(uvar[1, i] <= 1.0)
        # input cost
        cost += ca.mtimes(uvar[:, i].T,
                          ca.mtimes(mpc_cbf_param.matrix_R, uvar[:, i]))
    for i in range(mpc_cbf_param.num_of_horizon + 1):
        # speed vx upper bound
        opti.subject_to(xvar[0, i] <= 10.0)
        opti.subject_to(xvar[0, i] >= 0.0)
        # min and max of ey
        opti.subject_to(xvar[5, i] <= 2.0)
        opti.subject_to(-2.0 <= xvar[5, i])
        # state cost
        cost += ca.mtimes((xvar[:, i] - xtarget).T,
                          ca.mtimes(mpc_cbf_param.matrix_Q, xvar[:, i] - xtarget))
    # setup solver
    option = {"verbose": False, "ipopt.print_level": 0, "print_time": 0}
    opti.minimize(cost)
    opti.solver("ipopt", option)
    sol = opti.solve()
    end_timer = datetime.datetime.now()
    solver_time = (end_timer - start_timer).total_seconds()
    print("solver time: {}".format(solver_time))
    x_pred = sol.value(xvar).T
    u_pred = sol.value(uvar).T
    return u_pred[0, :]


def lmpc(xcurv, matrix_Atv, matrix_Btv, matrix_Ctv, xdim, udim,  ss_curv, Qfun, iter, lap_length, lap_width, u_old, lmpc_param):
    start_timer = datetime.datetime.now()
    ss_point_selected_tot = np.empty((xdim, 0))
    Qfun_selected_tot = np.empty((0))
    for jj in range(0, lmpc_param.num_ss_iter):
        ss_point_selected, Qfun_selected = lmpc_helper.select_points(
            ss_curv, Qfun, iter - jj - 1, xcurv, lmpc_param.num_ss_points / lmpc_param.num_ss_iter, lmpc_param.shift)
        ss_point_selected_tot = np.append(
            ss_point_selected_tot, ss_point_selected, axis=1)
        Qfun_selected_tot = np.append(Qfun_selected_tot, Qfun_selected, axis=0)
    # initialize the problem
    opti = ca.Opti()
    # define variables
    x = opti.variable(xdim, lmpc_param.num_of_horizon+1)
    u = opti.variable(udim, lmpc_param.num_of_horizon)
    lambd = opti.variable(Qfun_selected_tot.shape[0])
    slack = opti.variable(xdim)
    cost_mpc = 0
    cost_learning = 0
    # add constraints and cost function
    x_track = np.array([2.0, 0, 0, 0, 0, 0]).reshape(xdim, 1)
    opti.subject_to(x[:, 0] == xcurv)
    for i in range(lmpc_param.num_of_horizon):
        opti.subject_to(x[:, i+1] == mtimes(matrix_Atv[i], x[:, i]) +
                        mtimes(matrix_Btv[i], u[:, i]) + matrix_Ctv[i])
        # min and max of ey
        opti.subject_to(x[0, i] <= 3.0)
        opti.subject_to(x[5, i] <= lap_width)
        opti.subject_to(-lap_width <= x[5, i])
        # min and max of delta
        opti.subject_to(-0.5 <= u[0, i])
        opti.subject_to(u[0, i] <= 0.5)
        # min and max of a
        opti.subject_to(-1.0 <= u[1, i])
        opti.subject_to(u[1, i] <= 1.0)
        # quadratic cost
        cost_mpc += mtimes((x[:, i]-x_track).T,
                           mtimes(lmpc_param.matrix_Q, x[:, i]-x_track))
        cost_mpc += mtimes(u[:, i].T, mtimes(lmpc_param.matrix_R, u[:, i]))
        if i == 0:
            cost_mpc += mtimes((u[:, i]-u_old.T).T,
                               mtimes(lmpc_param.matrix_dR, u[:, i]-u_old.T))
        else:
            cost_mpc += mtimes((u[:, i]-u[:, i-1]).T,
                               mtimes(lmpc_param.matrix_dR, u[:, i]-u[:, i-1]))
    # convex hull for LMPC
    cost_mpc += mtimes((x[:, lmpc_param.num_of_horizon]-x_track).T,
                       mtimes(lmpc_param.matrix_Q, x[:, lmpc_param.num_of_horizon]-x_track))
    cost_learning += mtimes(slack.T, mtimes(lmpc_param.matrix_Qslack, slack))
    opti.subject_to(lambd >= np.zeros(lambd.shape[0]))
    opti.subject_to(x[:, lmpc_param.num_of_horizon] ==
                    mtimes(ss_point_selected_tot, lambd))
    opti.subject_to(mtimes(np.ones((1, lambd.shape[0])), lambd) == 1)
    opti.subject_to(mtimes(np.diag([1, 1, 1, 1, 1, 1]), slack) == np.zeros(6))
    cost_learning += mtimes(np.array([Qfun_selected_tot]), lambd)
    cost = cost_mpc + cost_learning
    option = {"verbose": False, "ipopt.print_level": 0, "print_time": 0}
    opti.minimize(cost)
    opti.solver("ipopt", option)
    try:
        sol = opti.solve()
        lin_points = np.concatenate(
            (sol.value(x).T[1:, :], np.array([sol.value(x).T[-1, :]])), axis=0)
        x_pred = sol.value(x).T
        u_pred = sol.value(u).T
        lin_input = np.vstack((u_pred[1:, :], u_pred[-1, :]))
    except RuntimeError:
        lin_points = np.concatenate((opti.debug.value(
            x).T[1:, :], np.array([opti.debug.value(x).T[-1, :]])), axis=0)
        x_pred = opti.debug.value(x).T
        u_pred = opti.debug.value(u).T
        lin_input = np.vstack((u_pred[1:, :], u_pred[-1, :]))
        print("solver fail to find the solution, the non-converged solution is used")
    end_timer = datetime.datetime.now()
    solver_time = (end_timer - start_timer).total_seconds()
    print("solver time: {}".format(solver_time))
    return u_pred, x_pred, ss_point_selected_tot, Qfun_selected_tot, lin_points, lin_input
