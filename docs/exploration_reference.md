# IN424 exploration reference

The supplied IN424 material describes autonomous exploration by two or three differential-drive ground robots in a ROS 2 and Gazebo exercise. The associated Python modules use a 2-D LiDAR occupancy map, map exchange/merge, frontier selection, A* grid planning, and `cmd_vel` path following. It is background for a possible EUDIS exploration lane; none of that mapping or path-planning code is part of this repository.

## What transfers conceptually

The useful separation is sensing, local map, shared evidence, candidate exploration target, path, and platform command. An exploration target can eventually become a task objective, but the current EUDIS task catalogue is preloaded and its lease/claim protocol controls **who may execute** a known task. It does not discover frontiers or maintain a shared occupancy map.

The IN424 implementation assumes ground-robot motion, 2-D LiDAR and structured environments. The supplied comparison note sketches aerial sensors, map patches, UAV-safe planning and MAVLink setpoints, but these are design possibilities, not validated components. Full-map exchange and a central map manager would also need an explicit delivery model before being described as partition-tolerant swarm behaviour.

## If exploration becomes an EUDIS milestone

Start with deterministic, pre-recorded candidate observations and test the boundary between local discovery, task creation, claim authority, and observer metrics. Measure duplicates and stale/disconnected evidence before choosing ROS 2, aerial mapping or flight-control integration. Preserve the existing deterministic simulator as the coordination baseline.

## Source and independence

Sources supplied on 29 September 2026: `IN424_Project_2026.pdf`, `In424_MultiRobotExploration_SA_ElYahchouchi_Lantres_Merce.zip`, and `UAV_Swarm_vs_IN424_Approach_Reference.pdf`. The comparison note was prepared for ADDITESS internal brainstorming. EUDIS is independent of ADDITESS; this note does not import the employer's work packages, platform requirements or project roles. No code from the supplied archive has been copied into this repository, and its reuse rights have not been established.
