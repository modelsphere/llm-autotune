"""The 3-layer control stack.

- search/        control layer: the space a campaign explores, and its validity
- orchestrator/  control layer: schedules runs, advances the run state machine
- launch/        execution layer: deployment drivers (ssh+docker now, k8s later)

Modularity rule: search never touches machines; the orchestrator is the
only component that spends resources; drivers hide *how* an engine service is
deployed behind one interface.
"""
