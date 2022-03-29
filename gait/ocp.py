import numpy as np
from casadi import vertcat, hcat, MX, sum1
from scipy.interpolate import interp1d
import biorbd_casadi
from bioptim import (
    OptimalControlProgram,
    DynamicsList,
    DynamicsFcn,
    BoundsList,
    QAndQDotBounds,
    InitialGuessList,
    ObjectiveList,
    ObjectiveFcn,
    InterpolationType,
    Node,
    ConstraintList,
    ConstraintFcn,
    PhaseTransitionList,
    PhaseTransitionFcn,
    PenaltyNode,
    BiorbdInterface,
    OdeSolver,
    Axis,
)


def get_contact_index(pn, tag):
    force_names = [s.to_string() for s in pn.nlp.model.contactNames()]
    return [i for i, t in enumerate([s[-1] == tag for s in force_names]) if t]

# --- track grf ---
def track_sum_contact_forces(pn: PenaltyNode) -> MX:
    """
    Adds the objective that the mismatch between the
    sum of the contact forces and the reference ground reaction forces should be minimized.

    Parameters
    ----------
    pn: PenaltyNode
        The penalty node elements

    Returns
    -------
    The cost that should be minimize in the MX format.
    """
    states = vertcat(pn.nlp.states["q"].mx, pn.nlp.states["qdot"].mx)
    controls = vertcat(pn.nlp.controls["tau"].mx, pn.nlp.controls["muscles"].mx)
    force_tp = pn.nlp.contact_forces_func(states, controls, pn.nlp.parameters.mx)

    force = vertcat(sum1(force_tp[get_contact_index(pn, "X"), :]),
                    sum1(force_tp[get_contact_index(pn, "Y"), :]),
                    sum1(force_tp[get_contact_index(pn, "Z"), :]))
    return BiorbdInterface.mx_to_cx("grf", force, pn.nlp.states["q"], pn.nlp.states["qdot"], pn.nlp.controls["tau"], pn.nlp.controls["muscles"])


def prepare_ocp(
    biorbd_model: tuple,
    final_time: list,
    nb_shooting: list,
    markers_ref: list,
    grf_ref: list,
    q_ref: list,
    qdot_ref: list,
    activation_ref : list,
    nb_threads: int,
    ode_solver=OdeSolver.RK4(),
) -> OptimalControlProgram:
    """
    Prepare the ocp

    Parameters
    ----------
    biorbd_model: tuple
        Tuple of bioMod (1 bioMod for each phase)
    final_time: list
        List of the time at the final node.
        The length of the list corresponds to the phase number
    nb_shooting: list
        List of the number of shooting points
    markers_ref: list
        List of the array of markers trajectories to track
    grf_ref: list
        List of the array of ground reaction forces to track
    q_ref: list
        List of the array of joint trajectories.
        Those trajectories were computed using Kalman filter
        They are used as initial guess
    qdot_ref: list
        List of the array of joint velocities.
        Those velocities were computed using Kalman filter
        They are used as initial guess
    nb_threads:int
        The number of threads used

    Returns
    -------
    The OptimalControlProgram ready to be solved
    """

    # Problem parameters
    nb_phases = len(biorbd_model)
    nb_q = biorbd_model[0].nbQ()
    nb_qdot = biorbd_model[0].nbQdot()
    nb_tau = biorbd_model[0].nbGeneralizedTorque()
    nb_mus = biorbd_model[0].nbMuscleTotal()

    min_bound, max_bound = 0, np.inf
    torque_min, torque_max, torque_init = -1000, 1000, 0
    activation_min, activation_max, activation_init = 1e-3, 1.0, 0.1

    # Add objective functions
    markers_pelvis = [0, 1, 2, 3] # ["L_IAS", "L_IPS", "R_IAS", "R_IPS"]
    markers_anat = [4, 9, 10, 11, 12, 17, 18] # ["R_FTC", "R_FLE", "R_FME", "R_FAX", "R_TTC", "R_FAL", "R_TAM"]
    markers_tissus = [5, 6, 7, 8, 13, 14, 15, 16]
    # ["R_Thigh_Top", "R_Thigh_Down", "R_Thigh_Front", "R_Thigh_Back", "R_Shank_Top", "R_Shank_Down", "R_Shank_Front", "R_Shank_Tibia"]
    markers_foot = [19, 20, 21, 22, 23, 24, 25] # ["R_FCC", "R_FM1", "R_FMP1", "R_FM2", "R_FMP2", "R_FM5", "R_FMP5"]
    markers_index = (markers_pelvis, markers_anat, markers_foot, markers_tissus)
    weight = (10000, 1000, 10000, 100)
    objective_functions = ObjectiveList()
    for p in range(nb_phases):
        for (i, m_idx) in enumerate(markers_index):
            objective_functions.add(
                ObjectiveFcn.Lagrange.TRACK_MARKERS,
                node=Node.ALL,
                weight=weight[i],
                marker_index=m_idx,
                target=markers_ref[p][:, m_idx, :],
                quadratic=True,
                phase=p,
            )
        objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_CONTROL, key="tau", weight=0.001, index=(10, 12), quadratic=True, phase=p)
        objective_functions.add(
            ObjectiveFcn.Lagrange.MINIMIZE_CONTROL, key="tau", weight=1, index=(6, 7, 8, 9, 11), phase=p, quadratic=True,
        )
        objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_CONTROL, key="muscles", weight=10, phase=p, quadratic=True,)
        objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_CONTROL, key="tau", derivative=True, weight=0.1, quadratic=True, phase=p)

    # --- track contact forces for the stance phase ---
    for p in range(nb_phases - 1):
        objective_functions.add(
            track_sum_contact_forces,  # track contact forces
            custom_type=ObjectiveFcn.Lagrange,
            target=grf_ref[p],
            node=Node.ALL,
            weight=0.01,
            quadratic=True,
            phase=p,
        )

    # Dynamics
    dynamics = DynamicsList()
    for p in range(nb_phases - 1):
        dynamics.add(DynamicsFcn.MUSCLE_DRIVEN, phase=p, with_contact=True, with_torque=True, expand=False)
    dynamics.add(DynamicsFcn.MUSCLE_DRIVEN, phase=3, with_torque=True, expand=False)

    # Constraints
    m_heel, m_m1, m_m5, m_toes = 26, 27, 28, 29
    constraints = ConstraintList()
    # null speed for the first phase --> non sliding contact point
    constraints.add(ConstraintFcn.TRACK_MARKERS_VELOCITY, node=Node.START, marker_index=m_heel, phase=0)
    # on the ground z=0
    constraints.add(ConstraintFcn.TRACK_MARKERS, node=Node.START, marker_index=m_heel, axes=Axis.Z, phase=0)

    # --- phase flatfoot ---
    Fz_heel, Fz_m1, Fx_m5, Fy_m5, Fz_m5 = 0, 1, 2, 3, 4
    # on the ground z=0
    constraints.add(ConstraintFcn.TRACK_MARKERS, node=Node.START, marker_index=[m_m1, m_m5], axes=Axis.Z, phase=1)
    constraints.add(  # positive vertical forces
        ConstraintFcn.TRACK_CONTACT_FORCES,
        min_bound=min_bound,
        max_bound=max_bound,
        node=Node.ALL,
        contact_index=(Fz_heel, Fz_m1, Fz_m5),
        phase=1,
    )
    constraints.add(  # non slipping
        ConstraintFcn.NON_SLIPPING,
        node=Node.ALL,
        tangential_component_idx=(Fx_m5, Fy_m5),
        normal_component_idx=(Fz_heel, Fz_m1, Fz_m5),
        static_friction_coefficient=0.5,
        phase=1,
    )
    constraints.add(  # forces heel at zeros at the end of the phase
        ConstraintFcn.TRACK_CONTACT_FORCES,
        node=Node.PENULTIMATE,
        contact_index=[i for i, name in enumerate(biorbd_model[1].contactNames()) if "Heel_r" in name.to_string()],
        phase=1,
    )

    # --- phase forefoot ---
    Fz_m1, Fx_m5, Fy_m5, Fz_m5, Fz_toe = 0, 1, 2, 3, 4
    constraints.add(  # positive vertical forces
        ConstraintFcn.TRACK_CONTACT_FORCES,
        min_bound=min_bound,
        max_bound=max_bound,
        node=Node.ALL,
        contact_index=(Fz_m1, Fz_m5, Fz_toe),
        phase=2,
    )
    constraints.add( # non slipping x m1
        ConstraintFcn.NON_SLIPPING,
        node=Node.ALL,
        tangential_component_idx=(Fx_m5, Fy_m5),
        normal_component_idx=(Fz_m1, Fz_m5, Fz_toe),
        static_friction_coefficient=0.5,
        phase=2,
    )

    # Phase Transitions
    phase_transitions = PhaseTransitionList()
    phase_transitions.add(PhaseTransitionFcn.IMPACT, phase_pre_idx=0)
    phase_transitions.add(PhaseTransitionFcn.IMPACT, phase_pre_idx=1)

    # Path constraint
    x_bounds = BoundsList()
    u_bounds = BoundsList()
    for p in range(nb_phases):
        x_bounds.add(bounds=QAndQDotBounds(biorbd_model[p]))
        u_bounds.add(
            [torque_min] * nb_tau + [activation_min] * nb_mus,
            [torque_max] * nb_tau + [activation_max] * nb_mus,
        )

    # Initial guess
    x_init = InitialGuessList()
    u_init = InitialGuessList()

    ocp_previous, sol_previous = OptimalControlProgram.load("gait.bo")
    for p in range(nb_phases):
        # init_x = np.zeros((nb_q + nb_qdot, nb_shooting[p] + 1))
        # init_x[:nb_q, :] = q_ref[p]
        # init_x[nb_q : nb_q + nb_qdot, :] = qdot_ref[p]
        # x_init.add(init_x, interpolation=InterpolationType.EACH_FRAME)

        x_init.add(sol_previous.states[p]['all'], interpolation=InterpolationType.EACH_FRAME)

        # init_u = np.zeros((nb_tau + nb_mus, nb_shooting[p]))
        # init_u[nb_tau:, :] = activation_ref[p][:, :-1]
        # u_init.add(init_u, interpolation=InterpolationType.EACH_FRAME)
        u_init.add(sol_previous.controls[p]['all'][:, :-1], interpolation=InterpolationType.EACH_FRAME)

    # ------------- #
    return OptimalControlProgram(
        biorbd_model,
        dynamics,
        nb_shooting,
        final_time,
        x_init,
        u_init,
        x_bounds,
        u_bounds,
        objective_functions,
        constraints,
        phase_transitions=phase_transitions,
        n_threads=nb_threads,
        ode_solver=ode_solver,
    )


def get_phase_time_shooting_numbers(data, dt):
    phase_time = data.c3d_data.get_time()
    number_shooting_points = []
    for time in phase_time:
        number_shooting_points.append(int(time / dt))
    return phase_time, number_shooting_points


def get_experimental_data(data, number_shooting_points, phase_time):
    q_ref = data.dispatch_data(data=data.q, nb_shooting=number_shooting_points, phase_time=phase_time)
    qdot_ref = data.dispatch_data(data=data.qdot, nb_shooting=number_shooting_points, phase_time=phase_time)
    markers_ref = data.dispatch_data(data=data.c3d_data.trajectories, nb_shooting=number_shooting_points, phase_time=phase_time)
    grf_ref = data.dispatch_data(data=data.c3d_data.forces, nb_shooting=number_shooting_points, phase_time=phase_time)
    moments_ref = data.dispatch_data(data=data.c3d_data.moments, nb_shooting=number_shooting_points, phase_time=phase_time)
    cop_ref = data.dispatch_data(data=data.c3d_data.cop, nb_shooting=number_shooting_points, phase_time=phase_time)
    emg_ref = data.dispatch_data(data=data.c3d_data.emg, nb_shooting=number_shooting_points, phase_time=phase_time)
    return q_ref, qdot_ref, markers_ref, grf_ref, moments_ref, cop_ref, emg_ref
