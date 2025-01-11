# EXPERIMENT=sim_interplan_DE_pdm
# worker=single_machine_thread_pool \
# worker.use_process_pool=True \
# worker=sequential \
# "/home/fyq/nuplan/exp/exp/kinematic/kinematic/2024.10.28.15.00.47/best_model/epoch\=0-step\=9199.ckpt" 
# /home/fyq/nuplan/exp/exp/train_DE_PDM/train_DE_PDM/2024.12.02.19.51.29/best_model/epoch\=0-step\=919.ckpt
EXPERIMENT=sim_diffusion_es_interplan
NUPLAN_EXP_ROOT=~/interplan_workspace/exp/
INTERPLAN_PLUGIN_ROOT=~/interplan_workspace/interplan-plugin

python $INTERPLAN_PLUGIN_ROOT/interplan/planning/script/run_simulation.py \
+simulation=default_interplan_benchmark \
planner=pdm_diffusion_planner \
planner.pdm_diffusion_planner.checkpoint_path="/home/fyq/nuplan/exp/exp/train_diffusion_model_256/train_DE_PDM/2024.12.11.17.39.43/best_model/epoch\=0-step\=919.ckpt" \
planner.pdm_diffusion_planner.dump_gifs_path="/home/fyq/nuplan/exp/viz_diffusion_es" \
scenario_filter=interplan10 \
experiment_name=$EXPERIMENT \
ego_controller=one_stage_controller \
worker=single_machine_thread_pool \
worker.use_process_pool=True \
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
