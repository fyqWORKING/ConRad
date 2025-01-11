EXPERIMENT=sim_diffusion_es_interplan
NUPLAN_EXP_ROOT=~/interplan_workspace/exp/
INTERPLAN_PLUGIN_ROOT=~/interplan_workspace/interplan-plugin

python $INTERPLAN_PLUGIN_ROOT/interplan/planning/script/run_simulation.py \
+simulation=default_interplan_benchmark \
planner=pdm_diffusion_planner \
planner.pdm_diffusion_planner.checkpoint_path="/home/fyq/nuplan/exp/exp/kinematic/kinematic/2024.10.28.15.00.47/best_model/epoch\=0-step\=9199.ckpt" \
planner.pdm_diffusion_planner.dump_gifs_path="/home/fyq/nuplan/exp/viz_diffusion_es" \
scenario_filter=interplan10 \
experiment_name=$EXPERIMENT \
planner.pdm_diffusion_planner.follow_centerline=False \
planner.pdm_diffusion_planner.scorer_config.ttc_fixed_speed=False \
ego_controller=one_stage_controller \
worker=sequential \
hydra.searchpath="[\
pkg://interplan.planning.script.config.common,\
pkg://interplan.planning.script.config.simulation,\
pkg://interplan.planning.script.experiments,\
pkg://tuplan_garage.planning.script.config.common,\
pkg://tuplan_garage.planning.script.config.simulation,\
pkg://nuplan.planning.script.config.common,\
pkg://nuplan.planning.script.config.simulation,\
pkg://nuplan.planning.script.experiments\
]"
