
CHALLENGE=closed_loop_reactive_agents # open_loop_boxes, closed_loop_nonreactive_agents, closed_loop_reactive_agents

python $NUPLAN_DEVKIT_ROOT/nuplan/planning/script/run_simulation.py \
+simulation=$CHALLENGE \
model=kinematic_diffusion_model \
planner=pdm_diffusion_planner \
planner.pdm_diffusion_planner.checkpoint_path="/home/fyq/nuplan/exp/exp/kinematic/kinematic/2024.10.28.15.00.47/best_model/epoch\=0-step\=9199.ckpt" \
planner.pdm_diffusion_planner.dump_gifs_path="/home/fyq/nuplan/exp/viz_diffusion_es" \
scenario_filter=nuplan_challenge_scenarios \
scenario_builder=nuplan \
# number_of_gpus_allocated_per_simulation=0.125 \
experiment_name=fyqtest1 \
hydra.searchpath="[pkg://tuplan_garage.planning.script.config.common, pkg://tuplan_garage.planning.script.config.simulation, pkg://nuplan.planning.script.config.common, pkg://nuplan.planning.script.experiments]" \
planner.pdm_diffusion_planner.follow_centerline=True \
planner.pdm_diffusion_planner.scorer_config.ttc_fixed_speed=False \
ego_controller=one_stage_controller \
# worker.threads_per_node=2
worker=sequential
